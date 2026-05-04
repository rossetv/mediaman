"""Migration v12: create ``login_failures`` table.

Persists brute-force tracking state across restarts.  Each row records the
failure count and lock-out deadline for a given username so that a process
restart does not silently reset a lockout in progress.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create the ``login_failures`` table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_failures (
            username TEXT PRIMARY KEY,
            failure_count INTEGER NOT NULL DEFAULT 0,
            first_failure_at TEXT,
            locked_until TEXT
        )
    """)
