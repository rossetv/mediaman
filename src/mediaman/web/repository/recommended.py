"""Repository for the recommended-routes ``suggestions`` table reads."""

from __future__ import annotations

import sqlite3

_SUGGESTIONS_COLS = (
    "id, title, year, media_type, category, tmdb_id, description, reason, "
    "poster_url, trailer_url, rating, rt_rating, tagline, runtime, genres, cast_json, "
    "director, trailer_key, imdb_rating, metascore, batch_id, downloaded_at, created_at"
)


# rationale: §9.5 permits a repository returning ``list[dict]`` at a
# documented template-feeding boundary. ``mediaman.web.routes.recommended.pages``
# groups these rows by ``batch_id`` and renders the dict keys straight onto the
# ``recommended.html`` Jinja template; a dataclass would add ceremony without
# removing the template's column coupling.
def fetch_recommendations(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return **all** cached recommendations from the DB for template rendering.

    Returns ``dict[str, object]`` rather than a dataclass: the template
    enumerates rows with ``.get(...)`` and the keys land on the Jinja
    surface unchanged. Callers that need pagination should use
    :func:`fetch_recommendations_page` instead.
    """
    rows = conn.execute(
        f"SELECT {_SUGGESTIONS_COLS} FROM suggestions ORDER BY batch_id DESC, category DESC, id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_recommendations_page(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, object]], int]:
    """Return a paginated page of recommendations plus the total row count.

    Pushes ``LIMIT``/``OFFSET`` into SQL so the full table is never loaded
    into memory for a single API request (§13.7).

    Returns ``(rows, total)`` where *rows* is at most *limit* dicts and
    *total* is the count of all rows matching the query.
    """
    total: int = conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]
    rows = conn.execute(
        f"SELECT {_SUGGESTIONS_COLS} FROM suggestions "
        "ORDER BY batch_id DESC, category DESC, id ASC "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(r) for r in rows], total


__all__ = ["fetch_recommendations", "fetch_recommendations_page"]
