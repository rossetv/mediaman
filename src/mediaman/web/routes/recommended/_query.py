"""Shared DB query helpers for the recommended routes package."""

from __future__ import annotations

import sqlite3


def fetch_recommendations(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return cached recommendations from the DB, ordered by type then insertion order."""
    rows = conn.execute("""
        SELECT id, title, year, media_type, category, tmdb_id, description, reason, poster_url, trailer_url, rating, rt_rating, tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore, batch_id, downloaded_at, created_at
        FROM suggestions ORDER BY batch_id DESC, category DESC, id ASC
    """).fetchall()
    return [dict(r) for r in rows]
