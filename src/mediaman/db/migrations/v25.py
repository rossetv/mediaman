"""Migration v25: prevent duplicate active deletions per media item.

A partial unique index on ``scheduled_actions`` ensures that only one
un-consumed pending deletion can exist for a given ``media_item_id`` at a
time.  Two concurrent scan engines racing the existing ``is_already_scheduled``
check would otherwise both insert a row before either sees the other's write.

Guarded: returns immediately if ``scheduled_actions`` does not exist.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Create a partial unique index preventing duplicate active deletion rows."""
    if not _table_exists(conn, "scheduled_actions"):
        return
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_scheduled_actions_unique_active_deletion "
        "ON scheduled_actions(media_item_id) "
        "WHERE action='scheduled_deletion' "
        "  AND token_used=0 "
        "  AND (delete_status IS NULL OR delete_status='pending')"
    )
