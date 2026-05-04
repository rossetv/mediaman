"""Migration v26: create ``newsletter_deliveries`` table.

The legacy newsletter flagged a scheduled item as notified after the first
successful Mailgun call, so a partial-failure run silently dropped
notifications for any later recipient.  This table records one row per
(scheduled_action, subscriber) so that each recipient's delivery can be
tracked independently.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create ``newsletter_deliveries`` with a per-action index."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS newsletter_deliveries (
            scheduled_action_id INTEGER NOT NULL,
            recipient TEXT NOT NULL,
            sent_at TEXT,
            error TEXT,
            attempted_at TEXT NOT NULL,
            PRIMARY KEY (scheduled_action_id, recipient)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_newsletter_deliveries_action "
        "ON newsletter_deliveries(scheduled_action_id)"
    )
