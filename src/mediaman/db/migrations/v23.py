"""Migration v23: create ``used_download_tokens`` table.

The original implementation cached consumed download-token hashes in an
in-process dict, so a process restart or a sibling worker would accept the
same one-shot link a second time.  This table persists hashes under a unique
primary key so the DB INSERT itself becomes the authoritative claim, mirroring
the ``keep_tokens_used`` table created in migration 18.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create ``used_download_tokens`` with an expiry index."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS used_download_tokens (
            token_hash TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL,
            used_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_used_download_tokens_expires "
        "ON used_download_tokens(expires_at)"
    )
