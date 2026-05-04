"""Migration v4: recreate ``suggestions`` table with extended columns.

The original ``suggestions`` table (if present) lacked several metadata
columns added in later sprints.  Rather than a series of ``ALTER TABLE``
statements, the table is dropped and recreated with the full target schema.
Any existing suggestion rows are discarded — at the time this migration ran,
suggestions were ephemeral recommendation data with no user-authored content.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Drop and recreate ``suggestions`` with the full extended column set."""
    conn.execute("DROP TABLE IF EXISTS suggestions")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            year INTEGER,
            media_type TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'personal',
            tmdb_id INTEGER,
            imdb_id TEXT,
            description TEXT,
            reason TEXT,
            poster_url TEXT,
            trailer_url TEXT,
            rating REAL,
            rt_rating TEXT,
            tagline TEXT,
            runtime INTEGER,
            genres TEXT,
            cast_json TEXT,
            director TEXT,
            trailer_key TEXT,
            imdb_rating TEXT,
            metascore TEXT,
            batch_id TEXT,
            downloaded_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
