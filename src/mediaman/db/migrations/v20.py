"""Migration v20: normalise ``subscribers.email`` to lowercase.

Deduplicates rows that differ only by email casing, retaining the row
with the lowest ``id`` for each normalised address, then lowercases all
email values and creates a case-insensitive unique index to prevent future
duplicates.

Guarded: returns immediately if the ``subscribers`` table does not exist.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Normalise subscriber emails to lowercase and add a case-insensitive unique index."""
    if not _table_exists(conn, "subscribers"):
        return
    conn.execute("""
        DELETE FROM subscribers
        WHERE id NOT IN (
            SELECT MIN(id) FROM subscribers GROUP BY LOWER(email)
        )
    """)
    conn.execute("UPDATE subscribers SET email = LOWER(email)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_email_nocase "
        "ON subscribers(email COLLATE NOCASE)"
    )
