"""Migration v31: add ``actor`` column to ``audit_log``.

Every existing audit row is anonymous: scanner-driven events and
admin-triggered events both land in ``audit_log`` with no first-class link to
the session that initiated them.  A dedicated nullable ``actor`` column lets
admin-triggered events attribute themselves to the responsible username and
makes "what did user X do?" answerable with a real WHERE clause.
Scanner-driven events leave the column NULL as the autonomous-action signal.

Guarded: returns immediately if ``audit_log`` does not exist.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists, _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``actor`` column to ``audit_log`` and create a partial index on it."""
    if not _table_exists(conn, "audit_log"):
        return
    if not _column_exists(conn, "audit_log", "actor"):
        conn.execute("ALTER TABLE audit_log ADD COLUMN actor TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor) WHERE actor IS NOT NULL"
    )
