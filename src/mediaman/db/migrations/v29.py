"""Migration v29: create ``delete_intents`` table for recoverable manual-delete.

A delete intent is written before the Radarr/Sonarr call so that if the
process crashes between the external call and the local DB cleanup, the
intent can be reconciled on startup via
``mediaman.web.routes.library.api.reconcile_pending_delete_intents``.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create ``delete_intents`` with a partial index on incomplete intents."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delete_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_item_id TEXT NOT NULL,
            target_kind TEXT NOT NULL,
            target_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            last_error TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_delete_intents_completed "
        "ON delete_intents(completed_at) WHERE completed_at IS NULL"
    )
