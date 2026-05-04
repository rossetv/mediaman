"""Migration v30: add ``claimed_at`` to ``download_notifications`` for crash-recovery.

The atomic claim flips ``notified=0 → notified=2`` to prevent two workers
from claiming the same row.  But a SIGKILL between the claim and the actual
send leaves rows stranded at ``notified=2`` because the in-process release
path only fires on Python exceptions.

This column lets a startup reconcile sweep reset rows whose claim is older
than the in-flight grace window back to ``notified=0`` so the next scheduler
tick retries them.

Guarded: returns immediately if ``download_notifications`` does not exist.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists, _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``claimed_at`` to ``download_notifications`` and create a partial index."""
    if not _table_exists(conn, "download_notifications"):
        return
    if not _column_exists(conn, "download_notifications", "claimed_at"):
        conn.execute("ALTER TABLE download_notifications ADD COLUMN claimed_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_download_notifications_claimed "
        "ON download_notifications(claimed_at) WHERE notified=2"
    )
