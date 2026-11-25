"""SQLite connection management and job-run helpers.

Schema DDL lives in mediaman.db.schema_definition and is applied
in full on every init_db call.

Connection lifecycle
--------------------
The bootstrap thread calls set_connection early in startup, registering
the connection it opened (via init_db) as the owning-thread connection.
Subsequent threads call get_db, which lazily opens a fresh per-thread
connection configured via _configure_connection — the owning-thread
connection takes precedence when the calling thread matches, and the thread-local
connection serves all other callers.  open_thread_connection is the
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
from pathlib import Path

from mediaman.core.time import now_iso, now_utc

from .schema_definition import SCHEMA

logger = logging.getLogger(__name__)


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")


# The removed migration runner stamped PRAGMA user_version; today's code
# never does, so a database it creates stays at 0. A stamp between 1 and
# 35 means the runner last saw it before the v36 schema and its upgrade
# never ran. These images are the last that still shipped each runner.
_LAST_SCHEMA_VERSION = 36
_LAST_PRE_SQUASH_VERSION = 34
_V36_RUNNER_IMAGE = "rossetv/mediaman:sha-8414ba2fff2979d5da43c5d37c4652057e9eecb6"
_V34_RUNNER_IMAGE = "rossetv/mediaman:sha-7fc9dcbb787b841c02d3ded522f7fbf67f3e41f1"


class SchemaTooOldError(RuntimeError):
    """The database predates schema v36 and this version cannot upgrade it."""


def _refuse_old_schema(db_path: str, version: int) -> None:
    if not 0 < version < _LAST_SCHEMA_VERSION:
        return
    if version < _LAST_PRE_SQUASH_VERSION:
        steps = f"{_V34_RUNNER_IMAGE} and then {_V36_RUNNER_IMAGE}, once each,"
        each, images = "each", "Those images are"
    else:
        steps = f"{_V36_RUNNER_IMAGE} once"
        each, images = "it", "That image is"
    raise SchemaTooOldError(
        f"Database {db_path} is at schema version {version}, older than "
        f"{_LAST_SCHEMA_VERSION}, and this version of mediaman cannot upgrade it. "
        f"It has not been changed. To upgrade it, start {steps} on the same "
        f"data folder, stopping {each} once it has finished starting up, then "
        f"start this version again. {images} built for amd64 only."
    )


def check_schema_version(db_path: str) -> None:
    """Raise ``SchemaTooOldError`` if the file at ``db_path`` is stamped 1-35.

    Opens the file read-only, so nothing is written. A missing file is a
    new install and passes.
    """
    path = Path(db_path)
    if not path.exists():
        return
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    _refuse_old_schema(db_path, version)


# Columns added to existing tables after the baseline schema. CREATE TABLE IF
# NOT EXISTS leaves an older table as it was, so each is added when missing.
_ADDED_COLUMNS = (
    ("media_items", "tmdb_id", "INTEGER"),
    ("media_items", "tvdb_id", "INTEGER"),
    ("media_items", "tracked_files", "TEXT"),
    ("media_items", "episode_count", "INTEGER"),
    ("media_items", "episode_keys", "TEXT"),
    ("media_items", "year", "INTEGER"),
    ("watch_progress", "seen_at", "TEXT"),
    ("suggestions", "backdrop_url", "TEXT"),
    ("scheduled_actions", "removed_files", "TEXT"),
    ("admin_users", "role", "TEXT NOT NULL DEFAULT 'admin'"),
    ("download_notifications", "season_number", "INTEGER"),
    ("download_notifications", "username", "TEXT"),
    ("delete_intents", "confirmed_at", "TEXT"),
    ("arr_searching_since", "last_seen", "TEXT"),
    ("plex_links", "libraries", "TEXT"),
    ("plex_links", "last_tried_at", "TEXT"),
    ("plex_plays", "library_id", "TEXT"),
    ("suggestions", "adult_checked", "INTEGER"),
    ("user_picks", "adult_checked", "INTEGER"),
    ("pruned_keeps", "tmdb_id", "INTEGER"),
    ("pruned_keeps", "plex_library_id", "INTEGER"),
    ("media_items", "last_tmdb_id", "INTEGER"),
    ("pruned_keeps", "tvdb_id", "INTEGER"),
    ("pruned_keeps", "given_libraries", "TEXT"),
    ("pruned_show_seasons", "tmdb_id", "INTEGER"),
    ("pruned_show_seasons", "tvdb_id", "INTEGER"),
    ("pruned_show_seasons", "plex_library_id", "INTEGER"),
    ("pruned_show_seasons", "given_libraries", "TEXT"),
    ("pruned_progress", "tmdb_id", "INTEGER"),
    ("pruned_progress", "tvdb_id", "INTEGER"),
    ("pruned_watches", "last_tmdb_id", "INTEGER"),
    ("media_items", "last_tvdb_id", "INTEGER"),
    ("media_items", "first_listed_at", "TEXT"),
    ("media_items", "unlisted", "INTEGER NOT NULL DEFAULT 0"),
    ("pruned_watches", "last_tvdb_id", "INTEGER"),
    ("pruned_keeps", "given_to", "TEXT"),
    ("pruned_show_seasons", "given_to", "TEXT"),
    ("pruned_keeps", "tracked_files", "TEXT"),
    ("pruned_show_seasons", "tracked_files", "TEXT"),
    ("pruned_watches", "tracked_files", "TEXT"),
)

# Not in SCHEMA, which runs before an older table has these columns.
_ADDED_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_media_items_tvdb_id
    ON media_items(COALESCE(tvdb_id, last_tvdb_id));
CREATE INDEX IF NOT EXISTS idx_media_items_tmdb_id
    ON media_items(COALESCE(tmdb_id, last_tmdb_id));
CREATE INDEX IF NOT EXISTS idx_pruned_watches_tvdb_id
    ON pruned_watches(COALESCE(tvdb_id, last_tvdb_id));
CREATE INDEX IF NOT EXISTS idx_pruned_watches_tmdb_id
    ON pruned_watches(COALESCE(tmdb_id, last_tmdb_id));
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, sql_type in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _fill_first_listed(conn: sqlite3.Connection) -> None:
    """Take an item stored without it as first listed when it was last seen.

    At every start: an older version that runs again stores items without it.
    """
    conn.execute(
        "UPDATE media_items SET first_listed_at = last_scanned_at WHERE first_listed_at IS NULL"
    )


def _take_over_plays(conn: sqlite3.Connection) -> None:
    """Fill played_titles from the plays of a database made before it."""
    rows = conn.execute(
        "SELECT wp.username, CASE WHEN wp.show_rating_key IS NULL THEN 'movie' ELSE 'tv' END,"
        " CASE WHEN wp.show_rating_key IS NULL THEN mi.title"
        " ELSE COALESCE(mi.show_title, mi.title) END,"
        " MAX(mi.tmdb_id), MAX(wp.credited_at)"
        " FROM watch_progress wp JOIN media_items mi ON mi.id = wp.media_item_id"
        " JOIN admin_users u ON u.username = wp.username"
        " WHERE wp.credited_at IS NOT NULL GROUP BY 1, 2, 3"
    ).fetchall()
    if rows:
        conn.executemany(
            "INSERT INTO played_titles (username, media_type, title, tmdb_id, played_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            rows,
        )


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialise the database, creating tables if needed.

    The full schema is applied unconditionally via
    ``CREATE … IF NOT EXISTS``, so the call is idempotent on an
    existing database.

    Raises:
        SchemaTooOldError: The database was last upgraded to a schema
            before v36. Nothing is written to it.
    """
    conn = sqlite3.connect(db_path)
    try:
        # Read before _configure_connection or the DDL, so a refused file is
        # not changed. Read-write, so a leftover rollback journal is replayed.
        _refuse_old_schema(db_path, conn.execute("PRAGMA user_version").fetchone()[0])
        _configure_connection(conn)
        _set_db_path(db_path)
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
        conn.executescript(_ADDED_INDEXES)
        _fill_first_listed(conn)
        _take_over_plays(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


_thread_local = threading.local()
# Single-process connection registry; mutated only from the bootstrap thread
# at startup and in test tear-down — no lock
# required because there is never concurrent mutation in production code.
_db_path: str | None = None
_owning_thread: int | None = None
_owning_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """Return a thread-local connection to the configured DB file.

    Raises:
        RuntimeError: If init_db has not been called and no connection
            has been registered via set_connection.
    """
    if _db_path is None and _owning_conn is None:
        raise RuntimeError("Database not initialised — call init_db first")

    if _owning_thread is not None and threading.get_ident() == _owning_thread:
        if _owning_conn is None:
            raise RuntimeError(
                "set_connection was called with a thread id but no connection — "
                "this is a bug in bootstrap."
            )
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
    global _owning_conn, _owning_thread
    _owning_conn = conn
    _owning_thread = threading.get_ident()


def reset_connection() -> None:
    """Drop the registered bootstrap connection and per-thread state.

    Tests autouse this between cases so a connection registered by one
    test does not leak into the next (the thread-local is process-wide
    and would otherwise survive across the test boundary). Production
    code must not call this — the only correct lifecycle is one
    init_db per process.
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
    global _db_path
    _db_path = path


def open_thread_connection(db_path: str) -> sqlite3.Connection:
    """Open a new SQLite connection configured for use in a background thread.

    Applies the standard pragmas (WAL mode, busy timeout, foreign keys, row
    factory) via _configure_connection.  The caller owns the returned
    connection and is responsible for closing it.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        A fully configured sqlite3.Connection.
    """
    conn = sqlite3.connect(db_path)
    _configure_connection(conn)
    return conn


def close_db() -> None:
    conn: sqlite3.Connection | None = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _thread_local.conn = None


# Heartbeat lease: a running job renews its ``heartbeat_at`` column at
# least once per _JOB_HEARTBEAT_INTERVAL_SECONDS. A row whose
# heartbeat is older than _JOB_HEARTBEAT_STALE_SECONDS is
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


def _get_job_owner_id() -> str:
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

    A row counts as alive when ``finished_at`` is unset and
    ``heartbeat_at`` is within the stale window. Crashed rows fall out
    so a new run can start cleanly.
    """
    _check_job_table(table)
    cutoff = (now_utc() - timedelta(seconds=_JOB_HEARTBEAT_STALE_SECONDS)).isoformat()
    row = conn.execute(
        f"SELECT id FROM {table} WHERE finished_at IS NULL AND heartbeat_at > ? LIMIT 1",
        (cutoff,),
    ).fetchone()
    return row is not None


def _start_job_run(conn: sqlite3.Connection, table: str) -> int | None:
    """Insert a new 'running' row in *table* and return its id.

    Stamps ``owner_id`` and ``heartbeat_at`` so subsequent
    heartbeat_job_run calls keep the row visible as live.

    Precondition: *conn* must not already have an open transaction.
    This function issues ``BEGIN IMMEDIATE`` directly; calling it inside
    an existing transaction raises ``sqlite3.OperationalError`` and leaves
    the outer transaction in an undefined state.
    """
    _check_job_table(table)
    # ``BEGIN IMMEDIATE`` obtains a reserved write lock upfront, preventing
    # SQLITE_BUSY when the INSERT races with another writer.  The ``with conn:``
    # context manager issues a deferred ``BEGIN`` instead, which does not
    # guarantee the write lock until the first write statement — a second
    # concurrent writer can acquire its own deferred lock and cause a collision.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _is_job_running(conn, table):
            conn.execute("ROLLBACK")
            return None
        now = now_iso()
        owner = _get_job_owner_id()
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
    enough — see _JOB_HEARTBEAT_INTERVAL_SECONDS) so a sibling
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
    return _is_job_running(conn, "scan_runs")


def start_scan_run(conn: sqlite3.Connection) -> int | None:
    """Begin a scan run. Returns the run id or None if one is already active.

    Must not be called inside an existing transaction — see _start_job_run.
    """
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
    return _is_job_running(conn, "refresh_runs")


def start_refresh_run(conn: sqlite3.Connection) -> int | None:
    """Begin a refresh run. Returns the run id or None if one is already active.

    Must not be called inside an existing transaction — see _start_job_run.
    """
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
