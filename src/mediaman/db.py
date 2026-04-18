"""SQLite database schema and connection management."""

import sqlite3
from pathlib import Path

DB_SCHEMA_VERSION = 10

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
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialise the database, creating tables if needed.

    Uses WAL mode for concurrent reads during web requests.
    Returns an open connection.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

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
    conn.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")
    conn.commit()

    return conn


def get_db() -> sqlite3.Connection:
    """Get the database connection. Set by main.py at startup."""
    if _connection is None:
        raise RuntimeError("Database not initialised — call init_db first")
    return _connection


_connection: sqlite3.Connection | None = None


def set_connection(conn: sqlite3.Connection) -> None:
    """Store the database connection for get_db()."""
    global _connection
    _connection = conn
