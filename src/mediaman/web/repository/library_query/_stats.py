"""Simple COUNT / SUM queries for the library stats bar.

Each function issues a single SQL query; the view-model assembler lives in
the library route.
"""

from __future__ import annotations

import sqlite3

from mediaman.web.repository.library_query._query import (
    ALL_SEASON_TYPES,
    ANIME_SEASON_TYPES,
    TV_SEASON_TYPES,
)


def count_movies(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM media_items WHERE media_type = 'movie'"
    ).fetchone()
    return int(row["n"])


def count_tv_shows(conn: sqlite3.Connection) -> int:
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
    """Return the count of stale *display items* — stale movies plus stale shows.

    Counts in the same unit as `count_movies` + `count_tv_shows`
    + `count_anime_shows` (and the library ``stale`` filter): movies
    are counted per row, but TV/anime seasons are grouped into one entry per
    show via ``COALESCE(show_rating_key, show_title)``.

    Counting raw ``media_items`` rows would let a single stale show with ten
    seasons contribute ten, pushing the stats-bar "Stale" number above the
    total item count.  A show is stale when it has at least one stale season,
    mirroring the ``stale`` filter's per-row WHERE + GROUP BY in
    `mediaman.web.repository.library_query._query._build_cte_sql`.
    """
    # rationale: placeholders are purely "?" * len(...) — no user value enters the SQL text
    season_placeholders = ",".join("?" * len(ALL_SEASON_TYPES))
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM (
            SELECT id FROM media_items
            WHERE media_type = 'movie'
              AND added_at < ?
              AND (last_watched_at IS NULL OR last_watched_at < ?)
            UNION ALL
            SELECT MIN(id) FROM media_items
            WHERE media_type IN ({season_placeholders})
              AND added_at < ?
              AND (last_watched_at IS NULL OR last_watched_at < ?)
            GROUP BY COALESCE(show_rating_key, show_title)
        )
        """,
        (age_cutoff, watch_cutoff, *ALL_SEASON_TYPES, age_cutoff, watch_cutoff),
    ).fetchone()
    return int(row["n"])


def sum_total_size_bytes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT SUM(file_size_bytes) AS n FROM media_items").fetchone()
    return int(row["n"] or 0)
