"""Migration v17: create ``arr_search_throttle`` table.

Persists the last-triggered timestamp for Radarr/Sonarr search calls so
that the scheduler can enforce a per-item cooldown across process restarts.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create the ``arr_search_throttle`` table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arr_search_throttle (
            key TEXT PRIMARY KEY,
            last_triggered_at TEXT NOT NULL
        )
    """)
