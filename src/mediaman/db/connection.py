"""SQLite connection management and job-run helpers.

Split from the original monolithic ``db.py`` (R5). Schema DDL and
migrations live in :mod:`mediaman.db.schema`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from mediaman.services.infra.time import now_iso

from .schema import apply_migrations

logger = logging.getLogger("mediaman")


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

    conn = getattr(_thread_local, "conn", None)
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
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _thread_local.conn = None


# Heartbeat lease (finding 9): a running job renews its ``heartbeat_at``
# column at least once per :data:`_JOB_HEARTBEAT_INTERVAL_SECONDS`. A row
# whose heartbeat (or, for legacy rows that pre-date migration 24, whose
# ``started_at``) is older than :data:`_JOB_HEARTBEAT_STALE_SECONDS` is
# considered crashed and no longer blocks new runs.
_JOB_HEARTBEAT_INTERVAL_SECONDS = 60
_JOB_HEARTBEAT_STALE_SECONDS = 5 * 60

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
    import os
    import socket as _sock

    try:
        host = _sock.gethostname()
    except Exception:
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
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=_JOB_HEARTBEAT_STALE_SECONDS)
    ).isoformat()
    row = conn.execute(
        f"SELECT id FROM {table} "  # noqa: S608 — table name from a fixed allow-list
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
            f"INSERT INTO {table} "  # noqa: S608 — table name from a fixed allow-list
            "(started_at, status, owner_id, heartbeat_at) "
            "VALUES (?, 'running', ?, ?)",
            (now, owner, now),
        )
        run_id = cursor.lastrowid
        conn.execute("COMMIT")
        return run_id
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
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
        f"UPDATE {table} SET finished_at=?, status=?, error=?, heartbeat_at=? "  # noqa: S608 — table name from a fixed allow-list
        "WHERE id=?",
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
            f"UPDATE {table} SET heartbeat_at=? WHERE id=? AND finished_at IS NULL",  # noqa: S608 — table name from a fixed allow-list
            (now_iso(), run_id),
        )
        conn.commit()
    except Exception:
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
