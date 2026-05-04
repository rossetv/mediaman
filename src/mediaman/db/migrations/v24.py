"""Migration v24: add ``owner_id`` and ``heartbeat_at`` to run tables.

Long-running scan or refresh jobs that exceeded the old fixed two-hour
cutoff would silently appear "stale" while still running, letting a second
worker start an overlapping job.  These columns let the running worker renew
its lease so that stale detection only fires when the heartbeat genuinely
lapses.

Applies to both ``scan_runs`` and ``refresh_runs``; each table is guarded
individually so the migration handles partial fixtures safely.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists, _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``owner_id`` and ``heartbeat_at`` columns to ``scan_runs`` and ``refresh_runs``."""
    for table in ("scan_runs", "refresh_runs"):
        if not _table_exists(conn, table):
            continue
        if not _column_exists(conn, table, "owner_id"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT")
        if not _column_exists(conn, table, "heartbeat_at"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN heartbeat_at TEXT")
