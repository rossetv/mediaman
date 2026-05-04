"""Migration v10: create ``ratings_cache`` table.

Caches IMDb / Rotten Tomatoes / Metascore ratings keyed on (tmdb_id,
media_type) so that the suggestions view does not re-fetch them on every
page load.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create the ``ratings_cache`` table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ratings_cache (
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            imdb_rating TEXT,
            rt_rating TEXT,
            metascore TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (tmdb_id, media_type)
        )
    """)
