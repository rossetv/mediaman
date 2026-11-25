"""Simple COUNT / SUM queries for the library stats bar.

Each function issues a single SQL query; the view-model assembler lives in
the library route.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from mediaman.core.format import format_bytes
from mediaman.core.library_selected import LIBRARY_SELECTED
from mediaman.core.removed_files import FILES_GONE
from mediaman.core.time import now_utc
from mediaman.scanner.runner import get_day_setting
from mediaman.web.repository.library_query._query import (
    ALL_SEASON_TYPES,
    ANIME_SEASON_TYPES,
    TV_SEASON_TYPES,
)

DUE_SOON_DAYS = 14


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


def count_titles(conn: sqlite3.Connection) -> dict[str, int]:
    """Return how many movies, TV shows and anime shows the library holds."""
    return {
        "movies": count_movies(conn),
        "tv": count_tv_shows(conn),
        "anime": count_anime_shows(conn),
    }


def count_no_size(conn: sqlite3.Connection) -> dict[str, int]:
    """Return how many movies, TV shows and anime shows have no size from Plex yet.

    A show counts when none of its seasons has one.
    """
    tv_marks = ",".join("?" * len(TV_SEASON_TYPES))
    anime_marks = ",".join("?" * len(ANIME_SEASON_TYPES))
    # rationale: placeholders are purely "?" * len(...) — no user value enters the SQL text
    show_sql = (
        "(SELECT COUNT(*) FROM (SELECT 1 FROM media_items WHERE media_type IN ({})"
        " GROUP BY COALESCE(show_rating_key, show_title) HAVING SUM(file_size_bytes) = 0))"
    )
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM media_items"
        " WHERE media_type = 'movie' AND file_size_bytes = 0) AS movies,"
        f" {show_sql.format(tv_marks)} AS tv, {show_sql.format(anime_marks)} AS anime",
        (*TV_SEASON_TYPES, *ANIME_SEASON_TYPES),
    ).fetchone()
    return {"movies": int(row["movies"]), "tv": int(row["tv"]), "anime": int(row["anime"])}


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


def sum_size_bytes_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    """Return the total file size of movies, TV and anime, in bytes."""
    result = {"movies": 0, "tv": 0, "anime": 0}
    for row in conn.execute(
        "SELECT media_type, SUM(file_size_bytes) AS n FROM media_items GROUP BY media_type"
    ):
        if row["media_type"] == "movie":
            key = "movies"
        elif row["media_type"] in TV_SEASON_TYPES:
            key = "tv"
        elif row["media_type"] in ANIME_SEASON_TYPES:
            key = "anime"
        else:
            continue
        result[key] += int(row["n"] or 0)
    return result


def count_deletions_due(conn: sqlite3.Connection, *, until: str) -> int:
    """Return the display items (movies, and shows once each) with a deletion due by *until*.

    An item whose library is not selected is not due: the deletion pass leaves it.
    """
    season_placeholders = ",".join("?" * len(ALL_SEASON_TYPES))
    # rationale: placeholders are purely "?" * len(...) and the condition a fixed string — no user value enters the SQL text
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM (
            SELECT mi.id FROM media_items mi
            WHERE mi.media_type = 'movie' AND {LIBRARY_SELECTED} AND EXISTS (
                SELECT 1 FROM scheduled_actions sa WHERE sa.media_item_id = mi.id
                AND sa.action = 'scheduled_deletion' AND sa.token_used = 0
                AND (sa.delete_status IS NULL OR sa.delete_status = 'pending')
                AND NOT {FILES_GONE}
                AND sa.execute_at <= ?)
            UNION ALL
            SELECT MIN(mi.id) FROM media_items mi
            WHERE mi.media_type IN ({season_placeholders}) AND {LIBRARY_SELECTED} AND EXISTS (
                SELECT 1 FROM scheduled_actions sa WHERE sa.media_item_id = mi.id
                AND sa.action = 'scheduled_deletion' AND sa.token_used = 0
                AND (sa.delete_status IS NULL OR sa.delete_status = 'pending')
                AND NOT {FILES_GONE}
                AND sa.execute_at <= ?)
            GROUP BY COALESCE(mi.show_rating_key, mi.show_title)
        )
        """,
        (until, *ALL_SEASON_TYPES, until),
    ).fetchone()
    return int(row["n"])


def fetch_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return counts, sizes, the stale count and the deletions due soon of the library.

    TV/anime totals group like fetch_library's display_items CTE, by
    COALESCE(show_rating_key, show_title). COUNT(DISTINCT NULL) counts 0 while the CTE's
    GROUP BY NULL collapses such rows into one group, so the count wraps a GROUP BY
    sub-select to make NULL count as 1.
    """
    titles = count_titles(conn)

    min_age = get_day_setting(conn, "min_age_days", default=30)
    inactivity = get_day_setting(conn, "inactivity_days", default=30)

    now = now_utc()
    age_cutoff = (now - timedelta(days=min_age)).isoformat()
    watch_cutoff = (now - timedelta(days=inactivity)).isoformat()

    stale = count_stale(conn, age_cutoff=age_cutoff, watch_cutoff=watch_cutoff)
    due_soon = count_deletions_due(conn, until=(now + timedelta(days=DUE_SOON_DAYS)).isoformat())

    sizes = sum_size_bytes_by_type(conn)
    total = sum(titles.values())
    total_size = format_bytes(sum_total_size_bytes(conn))

    return {
        **titles,
        "stale": stale,
        "stale_min_age": min_age,
        "total": total,
        "total_size": total_size,
        "movies_size": format_bytes(sizes["movies"]),
        "tv_size": format_bytes(sizes["tv"]),
        "anime_size": format_bytes(sizes["anime"]),
        "due_soon": due_soon,
        "due_soon_days": DUE_SOON_DAYS,
        "no_size": count_no_size(conn),
    }
