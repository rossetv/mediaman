"""0036: add nullable ``email`` column to ``admin_users``.

The download-notification scheduler emails the requester when a movie or
series finishes downloading. Before this migration, admin-initiated
requests stored the admin *username* in the notification's recipient
field — a placeholder from before the column was a real address — which
caused the Mailgun client to reject every send and re-queue the row
forever.

A NULL value means "no notification email set"; the route layer treats
that case as "do not record a download_notifications row" so the
scheduler never sees an undeliverable address.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Add ``email TEXT`` to ``admin_users`` (nullable, no default).

    Idempotent: skips the ALTER if the column already exists (e.g. when
    running against a DB that was initialised directly from the v36
    baseline ``_SCHEMA`` and then set to ``user_version=34`` in tests).
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(admin_users)").fetchall()
    }
    if "email" not in existing:
        conn.execute("ALTER TABLE admin_users ADD COLUMN email TEXT")
