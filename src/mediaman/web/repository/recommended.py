"""Repository for the recommended-routes ``suggestions`` table reads."""

from __future__ import annotations

import sqlite3


# rationale: §9.5 permits a repository returning ``list[dict]`` at a
# documented template-feeding boundary. ``mediaman.web.routes.recommended.pages``
# groups these rows by ``batch_id`` and renders the dict keys straight onto the
# ``recommended.html`` Jinja template (and ``recommended/api.py`` serialises the
# same dicts as JSON); a dataclass would add ceremony without removing the
# template's column coupling.
def fetch_recommendations(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return cached recommendations from the DB, ordered by type then insertion order.

    Returns ``dict[str, object]`` rather than a dataclass: the route
    layer enumerates the rows with ``.get(...)`` and the keys land on a
    Jinja template / JSON response unchanged, so wrapping the columns
    in a typed shape would add ceremony without removing coupling.
    """
    rows = conn.execute(
        "SELECT id, title, year, media_type, category, tmdb_id, description, reason, "
        "poster_url, trailer_url, rating, rt_rating, tagline, runtime, genres, cast_json, "
        "director, trailer_key, imdb_rating, metascore, batch_id, downloaded_at, created_at "
        "FROM suggestions "
        "ORDER BY batch_id DESC, category DESC, id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


__all__ = ["fetch_recommendations"]
