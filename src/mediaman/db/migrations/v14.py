"""Migration v14: add ``delete_status`` column to ``scheduled_actions``.

Tracks the lifecycle of a deletion (``pending`` → ``executing`` → ``done`` /
``failed``) so that crash-recovery logic can distinguish rows that were
never acted upon from those that were interrupted mid-flight.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists, _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``delete_status`` to ``scheduled_actions`` if the table and column are absent."""
    if not _table_exists(conn, "scheduled_actions"):
        return
    if not _column_exists(conn, "scheduled_actions", "delete_status"):
        conn.execute(
            "ALTER TABLE scheduled_actions ADD COLUMN delete_status TEXT NOT NULL DEFAULT 'pending'"
        )
