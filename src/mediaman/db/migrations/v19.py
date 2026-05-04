"""Migration v19: add ``ON DELETE CASCADE`` to ``admin_sessions.username`` FK.

SQLite does not support ``ALTER TABLE … ALTER COLUMN``, so the table must be
rebuilt using the rename-copy-drop pattern.  After the rebuild the
``idx_admin_sessions_expires`` index is recreated.

The migration is guarded: if ``admin_sessions`` does not exist (partial test
fixtures), the function returns immediately.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Rebuild ``admin_sessions`` to add ``ON DELETE CASCADE`` on the username FK."""
    if not _table_exists(conn, "admin_sessions"):
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("""
            CREATE TABLE admin_sessions_new (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL REFERENCES admin_users(username)
                    ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                token_hash TEXT,
                last_used_at TEXT,
                fingerprint TEXT,
                issued_ip TEXT
            )
        """)
        conn.execute("""
            INSERT INTO admin_sessions_new
                (token, username, created_at, expires_at,
                 token_hash, last_used_at, fingerprint, issued_ip)
            SELECT token, username, created_at, expires_at,
                   token_hash, last_used_at, fingerprint, issued_ip
            FROM admin_sessions
        """)
        conn.execute("DROP TABLE admin_sessions")
        conn.execute("ALTER TABLE admin_sessions_new RENAME TO admin_sessions")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires ON admin_sessions(expires_at)"
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
