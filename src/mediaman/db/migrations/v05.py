"""Migration v5: add ``rating`` and ``rt_rating`` columns to ``suggestions``.

These columns were omitted from the initial ``suggestions`` DDL and are
added here idempotently via ``ALTER TABLE``.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``rating`` and ``rt_rating`` to ``suggestions`` if absent."""
    if not _column_exists(conn, "suggestions", "rating"):
        conn.execute("ALTER TABLE suggestions ADD COLUMN rating REAL")
    if not _column_exists(conn, "suggestions", "rt_rating"):
        conn.execute("ALTER TABLE suggestions ADD COLUMN rt_rating TEXT")
