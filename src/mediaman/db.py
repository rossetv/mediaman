"""SQLite database schema and connection management.

Threading model
---------------

The database is accessed from multiple threads: the FastAPI worker
threadpool (web requests), the APScheduler worker threads (scans,
recommendation refreshes, download completion checks), and the main
startup thread. A single shared :class:`sqlite3.Connection` across all
of them is not safe — two concurrent writers on the same connection
can interleave their writes because sqlite3 serialises only at the
connection level, and ``conn.commit()`` in one thread will commit any
pending writes issued by another.

:func:`get_db` therefore returns a **per-thread** connection: each
thread lazily opens its own ``sqlite3.Connection`` to the same DB
file on first access. WAL mode handles file-level concurrency so
multiple readers can run while a single writer progresses. Schema
migration runs exactly once at startup on the main thread before any
other threads are spawned.
"""

import sqlite3
import threading
from pathlib import Path

DB_SCHEMA_VERSION = 12

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    encrypted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES admin_users(username),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    show_title TEXT,
    season_number INTEGER,
    plex_library_id INTEGER NOT NULL,
    plex_rating_key TEXT NOT NULL,
    sonarr_id INTEGER,
    radarr_id INTEGER,
    show_rating_key TEXT,
    added_at TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    poster_path TEXT,
    last_watched_at TEXT,
    last_scanned_at TEXT
);

CREATE TABLE IF NOT EXISTS scheduled_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id TEXT NOT NULL REFERENCES media_items(id),
    action TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    execute_at TEXT,
    token TEXT UNIQUE NOT NULL,
    token_used INTEGER NOT NULL DEFAULT 0,
    snoozed_at TEXT,
    snooze_duration TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    is_reentry INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    space_reclaimed_bytes INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kept_shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_rating_key TEXT NOT NULL UNIQUE,
    show_title TEXT NOT NULL,
    action TEXT NOT NULL,
    execute_at TEXT,
    snooze_duration TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    year INTEGER,
    media_type TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'personal',
    tmdb_id INTEGER,
    imdb_id TEXT,
    description TEXT,
    reason TEXT,
    poster_url TEXT,
    trailer_url TEXT,
    rating REAL,
    rt_rating TEXT,
    tagline TEXT,
    runtime INTEGER,
    genres TEXT,
    cast_json TEXT,
    director TEXT,
    trailer_key TEXT,
    imdb_rating TEXT,
    metascore TEXT,
    batch_id TEXT,
    downloaded_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ratings_cache (
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    imdb_rating TEXT,
    rt_rating TEXT,
    metascore TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (tmdb_id, media_type)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_actions_media
    ON scheduled_actions(media_item_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_execute
    ON scheduled_actions(execute_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_token
    ON scheduled_actions(token);
CREATE INDEX IF NOT EXISTS idx_audit_log_media
    ON audit_log(media_item_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_created
    ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires
    ON admin_sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_failures (
    username TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    first_failure_at TEXT,
    locked_until TEXT
);
"""


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pragmas every connection to this DB needs.

    Called for the bootstrap connection opened by :func:`init_db` and
    for each per-thread connection opened lazily by :func:`get_db`.
    WAL mode is idempotent at the file level; enabling it on every
    connection is cheap and ensures it survives ``PRAGMA`` resets.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialise the database, creating tables if needed.

    Uses WAL mode for concurrent reads during web requests. Returns
    the connection used for schema migration. The caller is expected
    to pass this connection to :func:`set_connection` so subsequent
    same-thread lookups via :func:`get_db` reuse it; other threads
    will lazily open their own connections to the same path.
    """
    conn = sqlite3.connect(db_path)
    _configure_connection(conn)
    # Record the DB path so other threads can lazily open their own
    # connections via ``get_db()`` without needing a reference to the
    # bootstrap connection.
    _set_db_path(db_path)

    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version < 1:
        conn.executescript(_SCHEMA)
    if current_version < 2:
        # Migration: add last_watched_at column to media_items
        cols = [r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()]
        if "last_watched_at" not in cols:
            conn.execute("ALTER TABLE media_items ADD COLUMN last_watched_at TEXT")
    if current_version < 3:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()]
        if "show_rating_key" not in cols:
            conn.execute("ALTER TABLE media_items ADD COLUMN show_rating_key TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kept_shows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                show_rating_key TEXT NOT NULL UNIQUE,
                show_title TEXT NOT NULL,
                action TEXT NOT NULL,
                execute_at TEXT,
                snooze_duration TEXT,
                created_at TEXT NOT NULL
            )
        """)
    if current_version < 4:
        # Drop and recreate suggestions table (safe — suggestions are regenerated)
        conn.execute("DROP TABLE IF EXISTS suggestions")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                year INTEGER,
                media_type TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'personal',
                tmdb_id INTEGER,
                imdb_id TEXT,
                description TEXT,
                reason TEXT,
                poster_url TEXT,
                trailer_url TEXT,
                rating REAL,
                rt_rating TEXT,
                tagline TEXT,
                runtime INTEGER,
                genres TEXT,
                cast_json TEXT,
                director TEXT,
                trailer_key TEXT,
                imdb_rating TEXT,
                metascore TEXT,
                batch_id TEXT,
                downloaded_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
    if current_version < 5:
        # Add rating columns missing from the v4 migration
        cols = [r[1] for r in conn.execute("PRAGMA table_info(suggestions)").fetchall()]
        if "rating" not in cols:
            conn.execute("ALTER TABLE suggestions ADD COLUMN rating REAL")
        if "rt_rating" not in cols:
            conn.execute("ALTER TABLE suggestions ADD COLUMN rt_rating TEXT")
    if current_version < 6:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(suggestions)").fetchall()]
        if "batch_id" not in cols:
            conn.execute("ALTER TABLE suggestions ADD COLUMN batch_id TEXT")
        if "downloaded_at" not in cols:
            conn.execute("ALTER TABLE suggestions ADD COLUMN downloaded_at TEXT")
        # Backfill batch_id from created_at date for existing rows
        conn.execute("UPDATE suggestions SET batch_id = DATE(created_at) WHERE batch_id IS NULL")
    if current_version < 7:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                title TEXT NOT NULL,
                media_type TEXT NOT NULL,
                tmdb_id INTEGER,
                service TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
    if current_version < 8:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(suggestions)").fetchall()]
        for col, col_type in [
            ("tagline", "TEXT"), ("runtime", "INTEGER"), ("genres", "TEXT"),
            ("cast_json", "TEXT"), ("director", "TEXT"), ("trailer_key", "TEXT"),
            ("imdb_rating", "TEXT"), ("metascore", "TEXT"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE suggestions ADD COLUMN {col} {col_type}")
    if current_version < 9:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recent_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dl_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'movie',
                poster_url TEXT DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
    if current_version < 10:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ratings_cache (
                tmdb_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                imdb_rating TEXT,
                rt_rating TEXT,
                metascore TEXT,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (tmdb_id, media_type)
            )
        """)
    if current_version < 11:
        # Sonarr series are identified by TVDB id, not TMDB. Previously
        # the Sonarr download completion path wrote the TVDB id into the
        # tmdb_id column, then compared it against each series' ``tmdbId``
        # in Sonarr's response — a field that is only populated when a
        # series was added via TMDB lookup. The mismatch meant TV
        # notifications never completed. Add a dedicated column so the
        # data model is honest, and migrate existing Sonarr rows.
        cols = [
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(download_notifications)"
            ).fetchall()
        ]
        if "tvdb_id" not in cols:
            conn.execute(
                "ALTER TABLE download_notifications ADD COLUMN tvdb_id INTEGER"
            )
        # Existing Sonarr rows: the value currently in tmdb_id is actually
        # the TVDB id. Move it across and clear the misnamed column.
        conn.execute(
            "UPDATE download_notifications "
            "SET tvdb_id = tmdb_id, tmdb_id = NULL "
            "WHERE service = 'sonarr' AND tvdb_id IS NULL"
        )
    if current_version < 12:
        # Per-username login lockout — persistent, so an attacker cannot
        # reset the counter by kicking the process over. See
        # ``mediaman.auth.login_lockout`` for the semantics.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_failures (
                username TEXT PRIMARY KEY,
                failure_count INTEGER NOT NULL DEFAULT 0,
                first_failure_at TEXT,
                locked_until TEXT
            )
        """)
    conn.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")
    conn.commit()

    return conn


_thread_local = threading.local()
_db_path: str | None = None
_owning_thread: int | None = None
_owning_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """Return a thread-local connection to the configured DB file.

    Called from web request handlers, scanner/scheduler threads, and
    tests. The first call on a thread lazily opens a dedicated
    connection; subsequent calls on the same thread reuse it. The
    bootstrap connection passed to :func:`set_connection` is returned
    verbatim for the thread that owns it so tests (which stash and
    reuse that same object directly) continue to see the familiar
    connection identity.
    """
    if _db_path is None and _owning_conn is None:
        raise RuntimeError("Database not initialised — call init_db first")

    # The bootstrap thread always gets the connection it registered —
    # existing test code passes it around by reference and expects
    # writes via that object to be visible via ``get_db()``.
    if _owning_thread is not None and threading.get_ident() == _owning_thread:
        assert _owning_conn is not None
        return _owning_conn

    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        return conn

    if _db_path is None:
        # Fallback path — ``set_connection`` was called with a conn but
        # no ``init_db`` beforehand (some test helpers). The only safe
        # thing we can do from another thread is reuse the owning one;
        # the call above already handled the owning-thread case.
        raise RuntimeError(
            "Cross-thread DB access requires init_db with a file path; "
            "connection was registered without a known path."
        )

    conn = sqlite3.connect(_db_path)
    _configure_connection(conn)
    _thread_local.conn = conn
    return conn


def set_connection(conn: sqlite3.Connection) -> None:
    """Register *conn* as the bootstrap connection for its thread.

    Stored for :func:`get_db` to hand back on the owning thread (the
    main app thread in production, the test thread in unit tests).
    Other threads that call :func:`get_db` will open their own
    connections to the same DB file via :func:`init_db`'s recorded
    path.
    """
    global _owning_conn, _owning_thread
    _owning_conn = conn
    _owning_thread = threading.get_ident()


def _set_db_path(path: str) -> None:
    """Record the DB path used for future per-thread connections."""
    global _db_path
    _db_path = path


def close_db() -> None:
    """Close the current thread's lazily-opened connection, if any.

    The bootstrap connection registered via :func:`set_connection`
    is left alone — it's owned by the caller that registered it.
    """
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _thread_local.conn = None
