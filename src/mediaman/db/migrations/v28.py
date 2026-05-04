"""Migration v28: make ``scheduled_actions.token`` nullable; backfill token hashes.

Keep tokens are now stored only as SHA-256 hashes.  This migration:

1. Ensures the ``token_hash`` column and unique index exist.
2. For any row where ``token_hash`` is NULL or empty and a real HMAC token is
   present, hashes the token and writes the hash.
3. Recreates ``scheduled_actions`` with ``token`` as nullable ``TEXT`` so
   future rows can store only the hash and omit the raw token.
4. Nulls out the raw token for rows that now carry a hash.
5. Re-creates the partial unique index added in migration 25 (the
   rename-copy-drop step drops it).

Guarded: returns immediately if ``scheduled_actions`` does not exist.
"""

from __future__ import annotations

import hashlib
import sqlite3

from mediaman.db.migrations._helpers import _column_exists, _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Rebuild ``scheduled_actions`` with nullable ``token``; backfill hashes."""
    if not _table_exists(conn, "scheduled_actions"):
        return
    if not _column_exists(conn, "scheduled_actions", "token_hash"):
        conn.execute("ALTER TABLE scheduled_actions ADD COLUMN token_hash TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_actions_token_hash "
            "ON scheduled_actions(token_hash) WHERE token_hash IS NOT NULL"
        )

    # Backfill hashes for rows that have a real HMAC token but no hash.
    rows_to_backfill = conn.execute(
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
        conn.execute(
            "UPDATE scheduled_actions SET token_hash = ? WHERE rowid = ?",
            (h, rid),
        )

    # Recreate scheduled_actions with token as nullable so future insertions
    # can omit the raw token once the hash is present.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("""
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
        conn.execute("""
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
        conn.execute("DROP TABLE scheduled_actions")
        conn.execute("ALTER TABLE scheduled_actions_v28 RENAME TO scheduled_actions")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_actions_media "
            "ON scheduled_actions(media_item_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_actions_execute "
            "ON scheduled_actions(execute_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_actions_token ON scheduled_actions(token)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_actions_token_hash "
            "ON scheduled_actions(token_hash) WHERE token_hash IS NOT NULL"
        )
        # Re-create the migration-25 partial unique index that the
        # table rename would have dropped.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_scheduled_actions_unique_active_deletion "
            "ON scheduled_actions(media_item_id) "
            "WHERE action='scheduled_deletion' "
            "  AND token_used=0 "
            "  AND (delete_status IS NULL OR delete_status='pending')"
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
