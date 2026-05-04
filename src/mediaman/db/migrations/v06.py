"""Migration v6: add ``batch_id`` and ``downloaded_at`` to ``suggestions``; backfill.

``batch_id`` groups suggestions fetched in a single recommendation run so
the UI can display them as a cohesive batch.  Existing rows are backfilled
using the date portion of ``created_at`` as a reasonable proxy.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``batch_id`` / ``downloaded_at`` and backfill ``batch_id`` for existing rows."""
    if not _column_exists(conn, "suggestions", "batch_id"):
        conn.execute("ALTER TABLE suggestions ADD COLUMN batch_id TEXT")
    if not _column_exists(conn, "suggestions", "downloaded_at"):
        conn.execute("ALTER TABLE suggestions ADD COLUMN downloaded_at TEXT")
    conn.execute("UPDATE suggestions SET batch_id = DATE(created_at) WHERE batch_id IS NULL")
