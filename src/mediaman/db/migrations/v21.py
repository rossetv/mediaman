"""Migration v21: add ``token_hash`` column to ``scheduled_actions``; backfill hashes.

Stores a SHA-256 hash of each action token so that token validity can be
checked without exposing the raw token.  Existing rows are backfilled.
A partial unique index is created on ``token_hash`` (excluding NULL rows)
to enforce hash uniqueness without requiring every row to carry a hash.

Guarded: returns immediately if ``scheduled_actions`` does not exist.

Note on positional access: the ``rowid`` alias produced by the SELECT is
accessed positionally (index 0) rather than by name because sqlite3.Row's
name look-up for the implicit rowid alias raises IndexError on some
databases.
"""

from __future__ import annotations

import hashlib
import sqlite3

from mediaman.db.migrations._helpers import _column_exists, _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add and backfill ``token_hash`` on ``scheduled_actions``."""
    if not _table_exists(conn, "scheduled_actions"):
        return
    if not _column_exists(conn, "scheduled_actions", "token_hash"):
        conn.execute("ALTER TABLE scheduled_actions ADD COLUMN token_hash TEXT")
    rows_to_hash = conn.execute(
        "SELECT rowid AS rid, token FROM scheduled_actions "
        "WHERE token_hash IS NULL AND token IS NOT NULL"
    ).fetchall()
    for _row in rows_to_hash:
        rid = _row[0]
        token_val = _row[1]
        h = hashlib.sha256(token_val.encode()).hexdigest()
        conn.execute(
            "UPDATE scheduled_actions SET token_hash = ? WHERE rowid = ?",
            (h, rid),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_actions_token_hash "
        "ON scheduled_actions(token_hash) WHERE token_hash IS NOT NULL"
    )
