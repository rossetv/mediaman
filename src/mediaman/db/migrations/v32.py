"""Migration v32: add hot-path indexes for media library and audit filtering.

Two complementary indexes for queries that previously fell back to a full
table scan:

* ``idx_media_items_plex_library_id`` — every scanner pass fans out a
  ``WHERE plex_library_id IN (...)`` against ``media_items``.
* ``idx_audit_log_action`` — the history view filters audit rows by ``action``
  heavily; the existing indexes cover ``media_item_id`` and ``created_at`` only.

``CREATE INDEX IF NOT EXISTS`` is idempotent.  Each table is guarded
individually so the migration handles partial test fixtures safely.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Create ``plex_library_id`` and ``action`` indexes where the tables exist."""
    if _table_exists(conn, "media_items"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_items_plex_library_id "
            "ON media_items(plex_library_id)"
        )
    if _table_exists(conn, "audit_log"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action)")
