"""Database schema constant and incremental migrations.

Split from the original monolithic ``db.py`` (R5). Connection lifecycle
belongs in :mod:`mediaman.db.connection`.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger("mediaman")

DB_SCHEMA_VERSION = 33

assert DB_SCHEMA_VERSION == 33, (
    f"DB_SCHEMA_VERSION is {DB_SCHEMA_VERSION} but the highest migration "
    "block is 33 — update one of them."
)

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
    is_reentry INTEGER NOT NULL DEFAULT 0,
    delete_status TEXT NOT NULL DEFAULT 'pending'
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
CREATE INDEX IF NOT EXISTS idx_audit_log_action
    ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires
    ON admin_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_media_items_plex_library_id
    ON media_items(plex_library_id);

-- Tamper-evidence on the audit log (v33). The triggers refuse UPDATE
-- and DELETE on existing rows so the only way to silently mutate the
-- log is to drop the trigger first — which itself shows up in
-- ``sqlite_master`` and is visible to any operator running ``.schema``.
-- INSERT remains unrestricted so the application code keeps writing
-- new rows normally.
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

CREATE TABLE IF NOT EXISTS login_failures (
    username TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    first_failure_at TEXT,
    locked_until TEXT
);

CREATE TABLE IF NOT EXISTS reauth_tickets (
    session_token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT
);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT
);
"""


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Run every migration block against *conn* up to :data:`DB_SCHEMA_VERSION`."""

    current_version = conn.execute("PRAGMA user_version").fetchone()[0]

    def _run_migration(target_version: int, body_fn) -> None:
        conn.execute("BEGIN")
        try:
            body_fn(conn)
            conn.execute(f"PRAGMA user_version={target_version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if current_version < 1:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.commit()

    if current_version < 2:

        def _v2(c: sqlite3.Connection) -> None:
            cols = [r[1] for r in c.execute("PRAGMA table_info(media_items)").fetchall()]
            if "last_watched_at" not in cols:
                c.execute("ALTER TABLE media_items ADD COLUMN last_watched_at TEXT")

        _run_migration(2, _v2)

    if current_version < 3:

        def _v3(c: sqlite3.Connection) -> None:
            cols = [r[1] for r in c.execute("PRAGMA table_info(media_items)").fetchall()]
            if "show_rating_key" not in cols:
                c.execute("ALTER TABLE media_items ADD COLUMN show_rating_key TEXT")
            c.execute("""
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

        _run_migration(3, _v3)

    if current_version < 4:

        def _v4(c: sqlite3.Connection) -> None:
            c.execute("DROP TABLE IF EXISTS suggestions")
            c.execute("""
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

        _run_migration(4, _v4)

    if current_version < 5:

        def _v5(c: sqlite3.Connection) -> None:
            cols = [r[1] for r in c.execute("PRAGMA table_info(suggestions)").fetchall()]
            if "rating" not in cols:
                c.execute("ALTER TABLE suggestions ADD COLUMN rating REAL")
            if "rt_rating" not in cols:
                c.execute("ALTER TABLE suggestions ADD COLUMN rt_rating TEXT")

        _run_migration(5, _v5)

    if current_version < 6:

        def _v6(c: sqlite3.Connection) -> None:
            cols = [r[1] for r in c.execute("PRAGMA table_info(suggestions)").fetchall()]
            if "batch_id" not in cols:
                c.execute("ALTER TABLE suggestions ADD COLUMN batch_id TEXT")
            if "downloaded_at" not in cols:
                c.execute("ALTER TABLE suggestions ADD COLUMN downloaded_at TEXT")
            c.execute("UPDATE suggestions SET batch_id = DATE(created_at) WHERE batch_id IS NULL")

        _run_migration(6, _v6)

    if current_version < 7:

        def _v7(c: sqlite3.Connection) -> None:
            c.execute("""
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

        _run_migration(7, _v7)

    if current_version < 8:

        def _v8(c: sqlite3.Connection) -> None:
            cols = [r[1] for r in c.execute("PRAGMA table_info(suggestions)").fetchall()]
            for col, col_type in [
                ("tagline", "TEXT"),
                ("runtime", "INTEGER"),
                ("genres", "TEXT"),
                ("cast_json", "TEXT"),
                ("director", "TEXT"),
                ("trailer_key", "TEXT"),
                ("imdb_rating", "TEXT"),
                ("metascore", "TEXT"),
            ]:
                if col not in cols:
                    c.execute(f"ALTER TABLE suggestions ADD COLUMN {col} {col_type}")

        _run_migration(8, _v8)

    if current_version < 9:

        def _v9(c: sqlite3.Connection) -> None:
            c.execute("""
                CREATE TABLE IF NOT EXISTS recent_downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dl_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'movie',
                    poster_url TEXT DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)

        _run_migration(9, _v9)

    if current_version < 10:

        def _v10(c: sqlite3.Connection) -> None:
            c.execute("""
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

        _run_migration(10, _v10)

    if current_version < 11:

        def _v11(c: sqlite3.Connection) -> None:
            cols = [r[1] for r in c.execute("PRAGMA table_info(download_notifications)").fetchall()]
            if "tvdb_id" not in cols:
                c.execute("ALTER TABLE download_notifications ADD COLUMN tvdb_id INTEGER")
            c.execute(
                "UPDATE download_notifications "
                "SET tvdb_id = tmdb_id, tmdb_id = NULL "
                "WHERE service = 'sonarr' AND tvdb_id IS NULL"
            )

        _run_migration(11, _v11)

    if current_version < 12:

        def _v12(c: sqlite3.Connection) -> None:
            c.execute("""
                CREATE TABLE IF NOT EXISTS login_failures (
                    username TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    first_failure_at TEXT,
                    locked_until TEXT
                )
            """)

        _run_migration(12, _v12)

    if current_version < 13:

        def _v13(c: sqlite3.Connection) -> None:
            has_sessions_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_sessions'"
                ).fetchone()
                is not None
            )
            has_users_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_users'"
                ).fetchone()
                is not None
            )
            session_cols: set[str] = set()
            if has_sessions_table:
                session_cols = {
                    row[1] for row in c.execute("PRAGMA table_info(admin_sessions)").fetchall()
                }
            if has_sessions_table:
                if "token_hash" not in session_cols:
                    c.execute("ALTER TABLE admin_sessions ADD COLUMN token_hash TEXT")
                if "last_used_at" not in session_cols:
                    c.execute("ALTER TABLE admin_sessions ADD COLUMN last_used_at TEXT")
                if "fingerprint" not in session_cols:
                    c.execute("ALTER TABLE admin_sessions ADD COLUMN fingerprint TEXT")
                if "issued_ip" not in session_cols:
                    c.execute("ALTER TABLE admin_sessions ADD COLUMN issued_ip TEXT")

            if has_users_table:
                user_cols = {
                    row[1] for row in c.execute("PRAGMA table_info(admin_users)").fetchall()
                }
                if "must_change_password" not in user_cols:
                    c.execute(
                        "ALTER TABLE admin_users ADD COLUMN "
                        "must_change_password INTEGER NOT NULL DEFAULT 0"
                    )

            if has_sessions_table:
                deleted_null = c.execute(
                    "DELETE FROM admin_sessions WHERE token_hash IS NULL OR token_hash = ''"
                ).rowcount
                if deleted_null:
                    logger.warning(
                        "db.migration_v13 purged_legacy_sessions count=%d reason=token_hash_missing",
                        deleted_null,
                    )
                cap = timedelta(days=1, seconds=60)
                _rows = c.execute(
                    "SELECT rowid AS rid, created_at, expires_at FROM admin_sessions "
                    "WHERE created_at IS NOT NULL AND expires_at IS NOT NULL"
                ).fetchall()
                stale_rowids: list[int] = []
                for _row in _rows:
                    try:
                        created = datetime.fromisoformat(_row[1])
                        expires = datetime.fromisoformat(_row[2])
                    except (TypeError, ValueError):
                        continue
                    if expires - created > cap:
                        stale_rowids.append(_row[0])
                if stale_rowids:
                    placeholders = ",".join("?" for _ in stale_rowids)
                    c.execute(
                        f"DELETE FROM admin_sessions WHERE rowid IN ({placeholders})",
                        stale_rowids,
                    )
                    logger.warning(
                        "db.migration_v13 purged_legacy_sessions count=%d reason=expiry_over_cap",
                        len(stale_rowids),
                    )

        _run_migration(13, _v13)

    if current_version < 14:

        def _v14(c: sqlite3.Connection) -> None:
            has_actions_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_actions'"
                ).fetchone()
                is not None
            )
            if has_actions_table:
                action_cols = {
                    row[1] for row in c.execute("PRAGMA table_info(scheduled_actions)").fetchall()
                }
                if "delete_status" not in action_cols:
                    c.execute(
                        "ALTER TABLE scheduled_actions ADD COLUMN "
                        "delete_status TEXT NOT NULL DEFAULT 'pending'"
                    )

        _run_migration(14, _v14)

    if current_version < 15:

        def _v15(c: sqlite3.Connection) -> None:
            c.execute("""
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    error TEXT
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS refresh_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    error TEXT
                )
            """)

        _run_migration(15, _v15)

    if current_version < 17:

        def _v17(c: sqlite3.Connection) -> None:
            c.execute("""
                CREATE TABLE IF NOT EXISTS arr_search_throttle (
                    key TEXT PRIMARY KEY,
                    last_triggered_at TEXT NOT NULL
                )
            """)

        _run_migration(17, _v17)

    if current_version < 18:

        def _v18(c: sqlite3.Connection) -> None:
            c.execute("""
                CREATE TABLE IF NOT EXISTS keep_tokens_used (
                    token_hash TEXT PRIMARY KEY,
                    used_at TEXT NOT NULL
                )
            """)

        _run_migration(18, _v18)

    if current_version < 19:

        def _v19(c: sqlite3.Connection) -> None:
            """Add ON DELETE CASCADE to admin_sessions.username FK (M13)."""
            has_sessions_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_sessions'"
                ).fetchone()
                is not None
            )
            if not has_sessions_table:
                return
            c.execute("PRAGMA foreign_keys=OFF")
            try:
                c.execute("""
                    CREATE TABLE admin_sessions_new (
                        token TEXT PRIMARY KEY,
                        username TEXT NOT NULL REFERENCES admin_users(username)
                            ON DELETE CASCADE,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        token_hash TEXT,
                        last_used_at TEXT,
                        fingerprint TEXT,
                        issued_ip TEXT
                    )
                """)
                c.execute("""
                    INSERT INTO admin_sessions_new
                        (token, username, created_at, expires_at,
                         token_hash, last_used_at, fingerprint, issued_ip)
                    SELECT token, username, created_at, expires_at,
                           token_hash, last_used_at, fingerprint, issued_ip
                    FROM admin_sessions
                """)
                c.execute("DROP TABLE admin_sessions")
                c.execute("ALTER TABLE admin_sessions_new RENAME TO admin_sessions")
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires "
                    "ON admin_sessions(expires_at)"
                )
            finally:
                c.execute("PRAGMA foreign_keys=ON")

        _run_migration(19, _v19)

    if current_version < 20:

        def _v20(c: sqlite3.Connection) -> None:
            """Normalise subscribers.email to lowercase on write (M14)."""
            has_subscribers = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscribers'"
                ).fetchone()
                is not None
            )
            if not has_subscribers:
                return
            c.execute("""
                DELETE FROM subscribers
                WHERE id NOT IN (
                    SELECT MIN(id) FROM subscribers GROUP BY LOWER(email)
                )
            """)
            c.execute("UPDATE subscribers SET email = LOWER(email)")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_email_nocase "
                "ON subscribers(email COLLATE NOCASE)"
            )

        _run_migration(20, _v20)

    if current_version < 21:

        def _v21(c: sqlite3.Connection) -> None:
            """Add token_hash column to scheduled_actions (M15)."""
            has_actions_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_actions'"
                ).fetchone()
                is not None
            )
            if not has_actions_table:
                return
            action_cols = {
                row[1] for row in c.execute("PRAGMA table_info(scheduled_actions)").fetchall()
            }
            if "token_hash" not in action_cols:
                c.execute("ALTER TABLE scheduled_actions ADD COLUMN token_hash TEXT")
            rows_to_hash = c.execute(
                "SELECT rowid AS rid, token FROM scheduled_actions "
                "WHERE token_hash IS NULL AND token IS NOT NULL"
            ).fetchall()
            for _row in rows_to_hash:
                # Positional access — sqlite3.Row's name lookup for the
                # implicit "rowid" alias was observed to raise IndexError
                # on some databases, so we alias the column explicitly and
                # fall back to tuple-indexing.
                rid = _row[0]
                token_val = _row[1]
                h = hashlib.sha256(token_val.encode()).hexdigest()
                c.execute(
                    "UPDATE scheduled_actions SET token_hash = ? WHERE rowid = ?",
                    (h, rid),
                )
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_actions_token_hash "
                "ON scheduled_actions(token_hash) WHERE token_hash IS NOT NULL"
            )

        _run_migration(21, _v21)

    if current_version < 22:

        def _v22(c: sqlite3.Connection) -> None:
            """Persist arr_search_throttle.search_count across restarts.

            Without it the in-memory counter resets on every deploy, so the
            "Searched N×" UI hint can hover at 1-2 forever even though the
            scheduler has poked Radarr/Sonarr for weeks.
            """
            cols = {
                row[1] for row in c.execute("PRAGMA table_info(arr_search_throttle)").fetchall()
            }
            if "search_count" not in cols:
                c.execute(
                    "ALTER TABLE arr_search_throttle "
                    "ADD COLUMN search_count INTEGER NOT NULL DEFAULT 0"
                )

        _run_migration(22, _v22)

    if current_version < 23:

        def _v23(c: sqlite3.Connection) -> None:
            """Persist consumed download token hashes (finding 2).

            The original implementation cached consumed tokens in an
            in-process dict, so a process restart — or a sibling worker —
            would happily accept the same one-shot link a second time.
            Persist the hash to SQLite under a unique constraint so the
            DB insert itself becomes the authoritative claim, mirroring
            ``keep_tokens_used`` (migration 18).
            """
            c.execute("""
                CREATE TABLE IF NOT EXISTS used_download_tokens (
                    token_hash TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    used_at TEXT NOT NULL
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_used_download_tokens_expires "
                "ON used_download_tokens(expires_at)"
            )

        _run_migration(23, _v23)

    if current_version < 24:

        def _v24(c: sqlite3.Connection) -> None:
            """Replace the fixed stale-job timeout with a heartbeat/lease.

            Long-running scan or refresh jobs that exceed the old fixed
            two-hour cutoff would silently appear "stale" while still
            running, letting a second worker start an overlapping job.
            Add an ``owner_id`` and ``heartbeat_at`` column to
            ``scan_runs`` and ``refresh_runs`` so the running worker can
            renew its lease and stale detection only fires when the
            heartbeat genuinely lapses.
            """
            for table in ("scan_runs", "refresh_runs"):
                has_table = (
                    c.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                    is not None
                )
                if not has_table:
                    continue
                cols = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
                if "owner_id" not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT")
                if "heartbeat_at" not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN heartbeat_at TEXT")

        _run_migration(24, _v24)

    if current_version < 25:

        def _v25(c: sqlite3.Connection) -> None:
            """Prevent duplicate active scheduled deletions per item (finding 9).

            A partial unique index on ``scheduled_actions`` ensures that
            only one un-consumed pending deletion can exist for a given
            ``media_item_id`` at a time. Two concurrent scan engines
            racing the existing ``is_already_scheduled`` check would
            otherwise both insert a row.
            """
            has_actions_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_actions'"
                ).fetchone()
                is not None
            )
            if not has_actions_table:
                return
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_scheduled_actions_unique_active_deletion "
                "ON scheduled_actions(media_item_id) "
                "WHERE action='scheduled_deletion' "
                "  AND token_used=0 "
                "  AND (delete_status IS NULL OR delete_status='pending')"
            )

        _run_migration(25, _v25)

    if current_version < 26:

        def _v26(c: sqlite3.Connection) -> None:
            """Track newsletter delivery per recipient (finding 23).

            The legacy newsletter flagged a scheduled item as notified
            after the first successful Mailgun call, so a partial-failure
            run silently dropped notifications for any later recipient.
            Record one row per (scheduled_action, subscriber) and only
            mark the action as notified once every recipient has either
            been delivered to or recorded its error.
            """
            c.execute("""
                CREATE TABLE IF NOT EXISTS newsletter_deliveries (
                    scheduled_action_id INTEGER NOT NULL,
                    recipient TEXT NOT NULL,
                    sent_at TEXT,
                    error TEXT,
                    attempted_at TEXT NOT NULL,
                    PRIMARY KEY (scheduled_action_id, recipient)
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_newsletter_deliveries_action "
                "ON newsletter_deliveries(scheduled_action_id)"
            )

        _run_migration(26, _v26)

    if current_version < 27:

        def _v27(c: sqlite3.Connection) -> None:
            """Add the reauth_tickets table backing recent-reauth gates.

            Owns the "this session reauthenticated at T" marker used by
            privilege-establishing endpoints (admin creation, sensitive
            settings, admin unlock, password change). Keyed on the session
            token hash so the row dies with the session via the helper-side
            revoke calls — a separate FK is intentionally not added because
            the admin_sessions row is already deleted-then-replaced by every
            session-rotation flow we run, and a hard FK would force callers
            to commit reauth state in the same transaction as the session
            row, which the pre-existing code paths don't do.
            """
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS reauth_tickets (
                    session_token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_reauth_tickets_username ON reauth_tickets(username)"
            )

        _run_migration(27, _v27)

    if current_version < 28:

        def _v28(c: sqlite3.Connection) -> None:
            """Backfill token_hash for keep tokens; make raw token column nullable.

            Finding 16: keep tokens are now stored only as SHA-256 hashes.
            This migration:
            1. Ensures the token_hash column and unique index exist.
            2. For any row where token_hash is NULL or empty and a real
               HMAC token is present, hashes the token and writes the hash.
            3. Recreates scheduled_actions with token as nullable TEXT so
               future rows inserted by keep.py and schedule_deletion() can
               store only the hash and omit the raw token.
            4. Nulls out the raw token for any rows that now have a hash.
            5. Re-creates the partial unique index added in migration 25
               (the rename-and-copy step would otherwise drop it).

            Guarded against partial test fixtures that hand-craft an older
            schema without ``scheduled_actions`` — in that case there is
            nothing to migrate.
            """
            has_actions_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_actions'"
                ).fetchone()
                is not None
            )
            if not has_actions_table:
                return
            action_cols = {
                row[1] for row in c.execute("PRAGMA table_info(scheduled_actions)").fetchall()
            }
            if "token_hash" not in action_cols:
                c.execute("ALTER TABLE scheduled_actions ADD COLUMN token_hash TEXT")
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_actions_token_hash "
                    "ON scheduled_actions(token_hash) WHERE token_hash IS NOT NULL"
                )

            # Backfill hashes for rows that have a real HMAC token but no hash.
            rows_to_backfill = c.execute(
                "SELECT rowid AS rid, token FROM scheduled_actions "
                "WHERE (token_hash IS NULL OR token_hash = '') "
                "AND token IS NOT NULL AND token != ''"
            ).fetchall()

            for row in rows_to_backfill:
                rid = row[0]
                token_val = row[1]
                # Skip placeholder tokens — they are not real HMAC tokens.
                if token_val.startswith("pending-"):
                    continue
                h = hashlib.sha256(token_val.encode()).hexdigest()
                c.execute(
                    "UPDATE scheduled_actions SET token_hash = ? WHERE rowid = ?",
                    (h, rid),
                )

            # Recreate scheduled_actions with token as nullable so future
            # insertions can omit the raw token once the hash is present.
            # Uses the standard SQLite rename-and-copy pattern.
            try:
                c.execute("PRAGMA foreign_keys=OFF")
                c.execute("""
                    CREATE TABLE IF NOT EXISTS scheduled_actions_v28 (
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
                    )
                """)
                c.execute("""
                    INSERT INTO scheduled_actions_v28
                        (id, media_item_id, action, scheduled_at, execute_at,
                         token, token_used, snoozed_at, snooze_duration,
                         notified, is_reentry, delete_status, token_hash)
                    SELECT id, media_item_id, action, scheduled_at, execute_at,
                           CASE WHEN token_hash IS NOT NULL AND token_hash != '' THEN NULL
                                ELSE token END,
                           token_used, snoozed_at, snooze_duration,
                           notified, is_reentry, delete_status, token_hash
                    FROM scheduled_actions
                """)
                c.execute("DROP TABLE scheduled_actions")
                c.execute("ALTER TABLE scheduled_actions_v28 RENAME TO scheduled_actions")
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scheduled_actions_media "
                    "ON scheduled_actions(media_item_id)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scheduled_actions_execute "
                    "ON scheduled_actions(execute_at)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scheduled_actions_token "
                    "ON scheduled_actions(token)"
                )
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_actions_token_hash "
                    "ON scheduled_actions(token_hash) WHERE token_hash IS NOT NULL"
                )
                # Re-create the migration-25 partial unique index that the
                # table rename would have dropped — without it, two
                # concurrent scans can re-insert duplicate active deletions.
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_scheduled_actions_unique_active_deletion "
                    "ON scheduled_actions(media_item_id) "
                    "WHERE action='scheduled_deletion' "
                    "  AND token_used=0 "
                    "  AND (delete_status IS NULL OR delete_status='pending')"
                )
            finally:
                c.execute("PRAGMA foreign_keys=ON")

        _run_migration(28, _v28)

    if current_version < 29:

        def _v29(c: sqlite3.Connection) -> None:
            """Add delete_intents table for recoverable manual-delete (finding 24).

            A delete intent is written before the Radarr/Sonarr call so that if
            the process crashes between the external call and the local DB cleanup,
            the intent can be reconciled on startup via
            :func:`mediaman.web.routes.library.api.reconcile_pending_delete_intents`.
            """
            c.execute("""
                CREATE TABLE IF NOT EXISTS delete_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_item_id TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_error TEXT
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_delete_intents_completed "
                "ON delete_intents(completed_at) WHERE completed_at IS NULL"
            )

        _run_migration(29, _v29)

    if current_version < 30:

        def _v30(c: sqlite3.Connection) -> None:
            """Add claimed_at to download_notifications for crash-recovery (H-5).

            The atomic claim added in migration 22 (finding 22) flips
            ``notified=0 → notified=2`` to prevent two workers from claiming
            the same row.  But a SIGKILL between the claim and the actual
            send leaves rows stranded at ``notified=2`` because the
            in-process release path only fires on Python exceptions.

            This column lets a startup reconcile sweep
            (:func:`mediaman.services.downloads.notifications.reconcile_stranded_notifications`)
            reset rows whose claim is older than the in-flight grace window
            back to ``notified=0`` so the next scheduler tick retries them.

            Idempotent: ``ALTER TABLE`` is guarded by a column existence
            check, and the whole block is guarded against partial test
            fixtures that hand-craft an older schema without
            ``download_notifications`` (matches the migration 28 pattern).
            """
            has_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='download_notifications'"
                ).fetchone()
                is not None
            )
            if not has_table:
                return
            cols = [
                row[1] for row in c.execute("PRAGMA table_info(download_notifications)").fetchall()
            ]
            if "claimed_at" not in cols:
                c.execute("ALTER TABLE download_notifications ADD COLUMN claimed_at TEXT")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_download_notifications_claimed "
                "ON download_notifications(claimed_at) WHERE notified=2"
            )

        _run_migration(30, _v30)

    if current_version < 31:

        def _v31(c: sqlite3.Connection) -> None:
            """Add ``actor`` column to ``audit_log`` (Domain 05 HIGH).

            Every existing audit row is anonymous: scanner-driven events
            and admin-triggered events both land in the same ``audit_log``
            with no first-class link to the session that initiated them.
            Security events embed ``actor=<user>`` into the ``detail``
            text via :func:`_format_security_body`, but it's a substring
            grep target, not a queryable column — and ``log_audit`` rows
            don't carry the field at all.

            Add a dedicated nullable ``actor`` column so admin-triggered
            events can attribute themselves to the responsible username,
            and so an operator can answer "what did user X do?" with a
            real WHERE clause. Scanner-driven events leave the column
            NULL (it's the autonomous-action signal).

            Idempotent: ``ALTER TABLE`` is guarded by a column existence
            check, and the whole block is guarded against partial test
            fixtures that hand-craft an older schema without
            ``audit_log``.
            """
            has_table = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'"
                ).fetchone()
                is not None
            )
            if not has_table:
                return
            cols = [row[1] for row in c.execute("PRAGMA table_info(audit_log)").fetchall()]
            if "actor" not in cols:
                c.execute("ALTER TABLE audit_log ADD COLUMN actor TEXT")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_log_actor "
                "ON audit_log(actor) WHERE actor IS NOT NULL"
            )

        _run_migration(31, _v31)

    if current_version < 32:

        def _v32(c: sqlite3.Connection) -> None:
            """Add hot-path indexes for media library + audit filtering (Domain 05).

            Two complementary indexes for queries that previously fell
            back to a full table scan:

            * ``idx_media_items_plex_library_id`` — every scanner pass
              fans out a ``WHERE plex_library_id IN (...)`` against
              ``media_items``. With no index the count grows linearly
              with library size on every call.
            * ``idx_audit_log_action`` — the history view filters audit
              rows by ``action`` heavily (notably the security-events
              query in ``web/routes/history.py``). Existing indexes
              cover ``media_item_id`` and ``created_at`` only.

            ``CREATE INDEX IF NOT EXISTS`` is idempotent so running the
            migration on a DB that already has either index is a
            no-op. Guarded against partial test fixtures that hand-craft
            an older schema without the underlying tables.
            """
            has_media_items = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_items'"
                ).fetchone()
                is not None
            )
            if has_media_items:
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_items_plex_library_id "
                    "ON media_items(plex_library_id)"
                )
            has_audit_log = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'"
                ).fetchone()
                is not None
            )
            if has_audit_log:
                c.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action)")

        _run_migration(32, _v32)

    if current_version < 33:

        def _v33(c: sqlite3.Connection) -> None:
            """Add tamper-evidence triggers to ``audit_log`` (Domain 05).

            The audit log is the operator's primary forensic surface — if
            an attacker with DB write access can silently UPDATE or DELETE
            rows, the trail is unreliable. Two triggers raise an SQLite
            error on any attempt to mutate or remove an existing row.
            INSERT remains free, so the application code that owns the
            log keeps writing new rows normally.

            The triggers are not a security boundary on their own —
            anyone with DB-file write access can drop the trigger first.
            They are a tamper-EVIDENCE measure: an audit row that has
            been silently mutated would otherwise look authentic; with
            the trigger in place the only way to mutate is to first drop
            the trigger, which itself shows up in ``sqlite_master`` and
            is visible to any operator who runs ``.schema``.
            """
            has_audit_log = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'"
                ).fetchone()
                is not None
            )
            if not has_audit_log:
                return
            c.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
            c.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
            c.execute(
                "CREATE TRIGGER audit_log_no_update "
                "BEFORE UPDATE ON audit_log "
                "BEGIN SELECT RAISE(ABORT, 'audit_log rows are append-only'); END"
            )
            c.execute(
                "CREATE TRIGGER audit_log_no_delete "
                "BEFORE DELETE ON audit_log "
                "BEGIN SELECT RAISE(ABORT, 'audit_log rows are append-only'); END"
            )

        _run_migration(33, _v33)
