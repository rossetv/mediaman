"""Migration v9: create ``recent_downloads`` table.

Persists a short-lived log of recently completed downloads so the dashboard
can surface them without a full Plex/arr scan.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create the ``recent_downloads`` table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recent_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dl_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'movie',
            poster_url TEXT DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
