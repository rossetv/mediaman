"""Migration v7: create ``download_notifications`` table.

Tracks per-email notifications for completed Radarr/Sonarr downloads so that
a confirmation e-mail can be sent exactly once per recipient per download event.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create the ``download_notifications`` table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            title TEXT NOT NULL,
            media_type TEXT NOT NULL,
            tmdb_id INTEGER,
            service TEXT NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
