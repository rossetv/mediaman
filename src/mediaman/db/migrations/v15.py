"""Migration v15: create ``scan_runs`` and ``refresh_runs`` tables.

Persists a history of background scan and refresh jobs so the UI can
display the last run time and status, and so a running job can assert
its ownership via heartbeat rows rather than a module-level lock.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create ``scan_runs`` and ``refresh_runs`` tables."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS refresh_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            error TEXT
        )
    """)
