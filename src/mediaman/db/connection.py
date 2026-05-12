"""SQLite connection management and job-run helpers.

Split from the original monolithic ``db.py`` (R5). Schema DDL and
migrations live in :mod:`mediaman.db.schema_definition` and
:mod:`mediaman.db.migrations`.

Connection lifecycle
--------------------
The bootstrap thread calls :func:`set_connection` early in startup, registering
the connection it opened (via :func:`init_db`) as the owning-thread connection.
Subsequent threads call :func:`get_db`, which lazily opens a fresh per-thread
connection configured via :func:`_configure_connection` — the owning-thread
connection takes precedence when the calling thread matches, and the thread-local
connection serves all other callers.  :func:`open_thread_connection` is the
explicit escape hatch for callers that need a brand-new, fully configured
connection outside the lazy-open mechanism.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import sqlite3
import threading
from datetime import timedelta

from mediaman.core.time import now_iso, now_utc

from .migrations import apply_migrations

logger = logging.getLogger(__name__)


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pragmas every connection to this DB needs."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialise the database, creating tables if needed."""
    conn = sqlite3.connect(db_path)
    _configure_connection(conn)
    _set_db_path(db_path)
    apply_migrations(conn)
    return conn


_thread_local = threading.local()
_db_path: str | None = None
_owning_thread: int | None = None
_owning_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """Return a thread-local connection to the configured DB file.

    Raises:
        RuntimeError: If :func:`init_db` has not been called and no connection
            has been registered via :func:`set_connection`.
    """
    if _db_path is None and _owning_conn is None:
        raise RuntimeError("Database not initialised — call init_db first")

    if _owning_thread is not None and threading.get_ident() == _owning_thread:
        assert _owning_conn is not None
        return _owning_conn

    conn: sqlite3.Connection | None = getattr(_thread_local, "conn", None)
    if conn is not None:
        return conn

    if _db_path is None:
        raise RuntimeError(
            "Cross-thread DB access requires init_db with a file path; "
            "connection was registered without a known path."
        )

    conn = sqlite3.connect(_db_path)
    _configure_connection(conn)
    _thread_local.conn = conn
    return conn


def set_connection(conn: sqlite3.Connection) -> None:
    """Register *conn* as the bootstrap connection for its thread."""
    global _owning_conn, _owning_thread
    _owning_conn = conn
    _owning_thread = threading.get_ident()


def reset_connection() -> None:
    """Drop the registered bootstrap connection and per-thread state.

    Tests autouse this between cases so a connection registered by one
    test does not leak into the next (the thread-local is process-wide
    and would otherwise survive across the test boundary). Production
    code must not call this — the only correct lifecycle is one
    :func:`init_db` per process.
    """
    global _owning_conn, _owning_thread, _db_path
    _owning_conn = None
    _owning_thread = None
    _db_path = None
    # Drop any lazily-opened thread-local connection too — leaving it
    # in place would let ``get_db()`` return a connection bound to a
    # database file from a previous test.
    conn: sqlite3.Connection | None = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error as exc:
            # Test reset must not raise: a connection that won't close
            # (already closed, locked from a prior crash) is the cost of
            # cleaning up; the next test opens a fresh one.
            logger.debug("reset_connection: close() raised %s — ignoring", exc)
        finally:
            _thread_local.conn = None


def _set_db_path(path: str) -> None:
    """Record the DB path used for future per-thread connections."""
    global _db_path
    _db_path = path


def open_thread_connection(db_path: str) -> sqlite3.Connection:
    """Open a new SQLite connection configured for use in a background thread.

    Applies the standard pragmas (WAL mode, busy timeout, foreign keys, row
    factory) via :func:`_configure_connection`.  The caller owns the returned
    connection and is responsible for closing it.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        A fully configured :class:`sqlite3.Connection`.
    """
    conn = sqlite3.connect(db_path)
    _configure_connection(conn)
    return conn


def close_db() -> None:
    """Close the current thread's lazily-opened connection, if any."""
    conn: sqlite3.Connection | None = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _thread_local.conn = None


# Heartbeat lease: a running job renews its ``heartbeat_at`` column at
# least once per :data:`_JOB_HEARTBEAT_INTERVAL_SECONDS`. A row whose
# heartbeat (or, for legacy rows that pre-date migration 24, whose
# ``started_at``) is older than :data:`_JOB_HEARTBEAT_STALE_SECONDS` is
# considered crashed and no longer blocks new runs.
_JOB_HEARTBEAT_INTERVAL_SECONDS = 60
_JOB_HEARTBEAT_STALE_SECONDS = 5 * 60
assert _JOB_HEARTBEAT_INTERVAL_SECONDS < _JOB_HEARTBEAT_STALE_SECONDS, (
    "heartbeat interval must be shorter than the stale threshold"
)

# Allow-list of job-run table names. Used to guard the internal helpers
# below so a future caller cannot accidentally interpolate
# attacker-controlled strings into the f-strings (B608 false positive).
_JOB_RUN_TABLES = frozenset({"scan_runs", "refresh_runs"})


def _check_job_table(table: str) -> None:
    if table not in _JOB_RUN_TABLES:
        raise ValueError(f"Unknown job-run table: {table!r}")


def _job_owner_id() -> str:
    """Return a per-process id used to attribute job-run rows.

    Uses the OS PID — strictly informational; we never compare owners
    when deciding whether to start a new run because the heartbeat is
    already an unforgeable liveness signal.
    """
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _is_job_running(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if a run row for *table* still has a live heartbeat.

    A row counts as alive when ``finished_at`` is unset and the most
    recent of ``heartbeat_at`` / ``started_at`` is within the stale
    window. Rows whose heartbeat (or started_at, for legacy entries)
    has lapsed are treated as crashed so a new run can start cleanly.
    """
    _check_job_table(table)
    cutoff = (now_utc() - timedelta(seconds=_JOB_HEARTBEAT_STALE_SECONDS)).isoformat()
    row = conn.execute(
        f"SELECT id FROM {table} "
        "WHERE finished_at IS NULL "
        "  AND COALESCE(heartbeat_at, started_at) > ? LIMIT 1",
        (cutoff,),
    ).fetchone()
    return row is not None


def _start_job_run(conn: sqlite3.Connection, table: str) -> int | None:
    """Insert a new 'running' row in *table* and return its id.

    Stamps ``owner_id`` and ``heartbeat_at`` so subsequent
    :func:`heartbeat_job_run` calls keep the row visible as live.
    """
    _check_job_table(table)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _is_job_running(conn, table):
            conn.execute("ROLLBACK")
            return None
        now = now_iso()
        owner = _job_owner_id()
        cursor = conn.execute(
            f"INSERT INTO {table} "
            "(started_at, status, owner_id, heartbeat_at) "
            "VALUES (?, 'running', ?, ?)",
            (now, owner, now),
        )
        run_id = cursor.lastrowid
        conn.execute("COMMIT")
        return run_id
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _finish_job_run(
    conn: sqlite3.Connection,
    table: str,
    run_id: int,
    status: str,
    error: str | None = None,
) -> None:
    """Mark *run_id* in *table* as finished with the given *status*."""
    _check_job_table(table)
    now = now_iso()
    conn.execute(
        f"UPDATE {table} SET finished_at=?, status=?, error=?, heartbeat_at=? WHERE id=?",
        (now, status, error, now, run_id),
    )
    conn.commit()


def _heartbeat_job_run(conn: sqlite3.Connection, table: str, run_id: int) -> None:
    """Stamp ``heartbeat_at`` for *run_id* with the current UTC time.

    Long-running scans should call this periodically (every minute is
    enough — see :data:`_JOB_HEARTBEAT_INTERVAL_SECONDS`) so a sibling
    invocation does not mistake the run for crashed and start an
    overlapping job. Best-effort: a transient lock failure is logged
    but not propagated.
    """
    _check_job_table(table)
    try:
        conn.execute(
            f"UPDATE {table} SET heartbeat_at=? WHERE id=? AND finished_at IS NULL",
            (now_iso(), run_id),
        )
        conn.commit()
    except sqlite3.Error:
        logger.warning("job heartbeat failed table=%s id=%s", table, run_id, exc_info=True)


def is_scan_running(conn: sqlite3.Connection) -> bool:
    """Return True if a scan job is currently active."""
    return _is_job_running(conn, "scan_runs")


def start_scan_run(conn: sqlite3.Connection) -> int | None:
    """Begin a scan run. Returns the run id or None if one is already active."""
    return _start_job_run(conn, "scan_runs")


def finish_scan_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    error: str | None = None,
) -> None:
    """Complete a scan run row."""
    _finish_job_run(conn, "scan_runs", run_id, status, error)


def heartbeat_scan_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Renew a scan run's heartbeat so the lease stays live."""
    _heartbeat_job_run(conn, "scan_runs", run_id)


def is_refresh_running(conn: sqlite3.Connection) -> bool:
    """Return True if a recommendation-refresh job is currently active."""
    return _is_job_running(conn, "refresh_runs")


def start_refresh_run(conn: sqlite3.Connection) -> int | None:
    """Begin a refresh run. Returns the run id or None if one is already active."""
    return _start_job_run(conn, "refresh_runs")


def finish_refresh_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    error: str | None = None,
) -> None:
    """Complete a refresh run row."""
    _finish_job_run(conn, "refresh_runs", run_id, status, error)


def heartbeat_refresh_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Renew a refresh run's heartbeat so the lease stays live."""
    _heartbeat_job_run(conn, "refresh_runs", run_id)
