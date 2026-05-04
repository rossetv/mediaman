"""Migration v2: add ``last_watched_at`` column to ``media_items``.

Earlier databases were created before watch-time tracking was introduced.
The column is added with ``ALTER TABLE`` only when absent so the migration
is safe to re-run.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``last_watched_at TEXT`` to ``media_items`` if not already present."""
    if not _column_exists(conn, "media_items", "last_watched_at"):
        conn.execute("ALTER TABLE media_items ADD COLUMN last_watched_at TEXT")
