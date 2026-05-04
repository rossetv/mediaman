"""Migration v3: add ``show_rating_key`` to ``media_items``; create ``kept_shows``.

Introduces TV-show tracking: a foreign-key-free rating key on each episode
row and a ``kept_shows`` table that records shows the operator has elected to
retain or snooze.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``show_rating_key`` column and create ``kept_shows`` table."""
    if not _column_exists(conn, "media_items", "show_rating_key"):
        conn.execute("ALTER TABLE media_items ADD COLUMN show_rating_key TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kept_shows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_rating_key TEXT NOT NULL UNIQUE,
            show_title TEXT NOT NULL,
            action TEXT NOT NULL,
            execute_at TEXT,
            snooze_duration TEXT,
            created_at TEXT NOT NULL
        )
    """)
