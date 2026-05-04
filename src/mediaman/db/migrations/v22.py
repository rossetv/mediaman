"""Migration v22: persist ``search_count`` in ``arr_search_throttle``.

Without this column the in-memory search counter reset on every deploy,
causing the "Searched N×" UI hint to hover at 1-2 indefinitely even after
weeks of scheduler activity.  The column is added with ``ALTER TABLE``
only when absent so the migration is idempotent.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``search_count`` to ``arr_search_throttle`` if absent."""
    if not _column_exists(conn, "arr_search_throttle", "search_count"):
        conn.execute(
            "ALTER TABLE arr_search_throttle ADD COLUMN search_count INTEGER NOT NULL DEFAULT 0"
        )
