"""Simple COUNT / SUM queries for the library stats bar.

Each function issues a single SQL query; the view-model assembler lives in
the library route.
"""

from __future__ import annotations

import sqlite3

from mediaman.web.repository.library_query._query import ANIME_SEASON_TYPES, TV_SEASON_TYPES


def count_movies(conn: sqlite3.Connection) -> int:
    """Return the count of movie rows in ``media_items``."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM media_items WHERE media_type = 'movie'"
    ).fetchone()
    return int(row["n"])


def count_tv_shows(conn: sqlite3.Connection) -> int:
    """Return the count of distinct TV shows (grouped by show_rating_key/show_title)."""
    # rationale: placeholders is purely "?" * len(TV_SEASON_TYPES) — no user value ever enters the SQL text
    placeholders = ",".join("?" * len(TV_SEASON_TYPES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM ("
        f"  SELECT 1 FROM media_items "
        f"  WHERE media_type IN ({placeholders}) "
        f"  GROUP BY COALESCE(show_rating_key, show_title)"
        f")",
        TV_SEASON_TYPES,
    ).fetchone()
    return int(row["n"])


def count_anime_shows(conn: sqlite3.Connection) -> int:
    """Return the count of distinct anime shows (grouped by show_rating_key/show_title)."""
    # rationale: placeholders is purely "?" * len(ANIME_SEASON_TYPES) — no user value ever enters the SQL text
    placeholders = ",".join("?" * len(ANIME_SEASON_TYPES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM ("
        f"  SELECT 1 FROM media_items "
        f"  WHERE media_type IN ({placeholders}) "
        f"  GROUP BY COALESCE(show_rating_key, show_title)"
        f")",
        ANIME_SEASON_TYPES,
    ).fetchone()
    return int(row["n"])


def count_stale(conn: sqlite3.Connection, *, age_cutoff: str, watch_cutoff: str) -> int:
    """Return the count of media items older than *age_cutoff* and unwatched since *watch_cutoff*."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM media_items
        WHERE added_at < ?
          AND (last_watched_at IS NULL OR last_watched_at < ?)
        """,
        (age_cutoff, watch_cutoff),
    ).fetchone()
    return int(row["n"])


def sum_total_size_bytes(conn: sqlite3.Connection) -> int:
    """Return the total file_size_bytes across all media items (0 when empty)."""
    row = conn.execute("SELECT SUM(file_size_bytes) AS n FROM media_items").fetchone()
    return int(row["n"] or 0)
