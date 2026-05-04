"""Migration v27: create ``reauth_tickets`` table.

Owns the "this session reauthenticated at T" marker used by
privilege-establishing endpoints (admin creation, sensitive settings, admin
unlock, password change).  Keyed on the session token hash so the row dies
with the session via the helper-side revoke calls.

A hard FK on ``admin_sessions`` is intentionally omitted because the
``admin_sessions`` row is already deleted-then-replaced by every
session-rotation flow, and a hard FK would force callers to commit reauth
state in the same transaction as the session row.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create ``reauth_tickets`` with a username index."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reauth_tickets (
            session_token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reauth_tickets_username ON reauth_tickets(username)"
    )
