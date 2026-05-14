"""Full schema for a fresh mediaman database.

A single DDL script applied by :func:`mediaman.db.init_db` on every open.
All statements are ``CREATE … IF NOT EXISTS`` so opening an already-
populated database is a no-op.
"""

from __future__ import annotations

SCHEMA = """
-- === Settings / encrypted KV ===
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    encrypted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- === Authentication (users, sessions, login attempts, lockouts) ===
CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    email TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES admin_users(username) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    token_hash TEXT,
    last_used_at TEXT,
    fingerprint TEXT,
    issued_ip TEXT
);

-- === Scanner (media items, scheduled actions, kept shows, audit log) ===
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
    token TEXT UNIQUE,
    token_used INTEGER NOT NULL DEFAULT 0,
    snoozed_at TEXT,
    snooze_duration TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    is_reentry INTEGER NOT NULL DEFAULT 0,
    delete_status TEXT NOT NULL DEFAULT 'pending',
    token_hash TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    space_reclaimed_bytes INTEGER,
    created_at TEXT NOT NULL,
    actor TEXT
);

-- === Newsletter (subscribers, delivery tracking) ===
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

-- === Recommendations (cached suggestions, ratings cache) ===
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

-- === Downloads (NZBGet matches, notifications, redownload audits) ===
CREATE TABLE IF NOT EXISTS download_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER,
    service TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    tvdb_id INTEGER,
    claimed_at TEXT
);

CREATE TABLE IF NOT EXISTS recent_downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dl_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'movie',
    poster_url TEXT DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- === Throttles and tokens ===
CREATE TABLE IF NOT EXISTS login_failures (
    username TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    first_failure_at TEXT,
    locked_until TEXT
);

CREATE TABLE IF NOT EXISTS arr_search_throttle (
    key TEXT PRIMARY KEY,
    last_triggered_at TEXT NOT NULL,
    search_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS keep_tokens_used (
    token_hash TEXT PRIMARY KEY,
    used_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS used_download_tokens (
    token_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    used_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    owner_id TEXT,
    heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    owner_id TEXT,
    heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS newsletter_deliveries (
    scheduled_action_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    sent_at TEXT,
    error TEXT,
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (scheduled_action_id, recipient)
);

CREATE TABLE IF NOT EXISTS reauth_tickets (
    session_token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delete_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_scheduled_actions_media ON scheduled_actions(media_item_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_execute ON scheduled_actions(execute_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_token ON scheduled_actions(token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_actions_token_hash ON scheduled_actions(token_hash) WHERE token_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_actions_unique_active_deletion ON scheduled_actions(media_item_id) WHERE action='scheduled_deletion' AND token_used=0 AND (delete_status IS NULL OR delete_status='pending');
CREATE INDEX IF NOT EXISTS idx_audit_log_media ON audit_log(media_item_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor) WHERE actor IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires ON admin_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_media_items_plex_library_id ON media_items(plex_library_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_email_nocase ON subscribers(email COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_used_download_tokens_expires ON used_download_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_newsletter_deliveries_action ON newsletter_deliveries(scheduled_action_id);
CREATE INDEX IF NOT EXISTS idx_reauth_tickets_username ON reauth_tickets(username);
CREATE INDEX IF NOT EXISTS idx_delete_intents_completed ON delete_intents(completed_at) WHERE completed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_download_notifications_claimed ON download_notifications(claimed_at) WHERE notified=2;

-- Tamper-evidence triggers on audit_log. UPDATE and DELETE are forbidden;
-- dropping the trigger to bypass them shows up in sqlite_master and is
-- visible to any operator running .schema. INSERT remains unrestricted.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log rows are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log rows are append-only');
END;
"""
