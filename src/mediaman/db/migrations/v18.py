"""Migration v18: create ``keep_tokens_used`` table.

Records consumed keep-token hashes under a unique constraint so that a
one-shot keep link cannot be replayed after process restart or across
multiple worker instances.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create the ``keep_tokens_used`` table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keep_tokens_used (
            token_hash TEXT PRIMARY KEY,
            used_at TEXT NOT NULL
        )
    """)
