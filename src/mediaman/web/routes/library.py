"""Library page routes.

Handles the browser-facing GET /library page.  All JSON API endpoints
(``/api/library``, ``/api/media/…``) live in the sibling module
:mod:`mediaman.web.routes.library_api`.

Query helpers (``fetch_library``, private helpers) and the shared
constants (``_VALID_SORTS``, ``_VALID_TYPES``, ``TV_SEASON_TYPES``,
etc.) now live in the canonical location
:mod:`mediaman.scanner.repository.library_query` so that
:mod:`mediaman.web.routes.library_api` can import them without
creating a peer-route import (§2.8.6).  This module re-exports them
so existing external importers (tests, app_factory) continue to work.

The private names remain importable under the old
``mediaman.web.routes.library._query`` path via the ``_query``
attribute shim at the bottom of this file so existing tests that
import directly from that path continue to work.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from mediaman.core.format import format_bytes
from mediaman.core.time import now_utc
from mediaman.scanner.repository.library_query import (
    _MAX_SEARCH_TERM_LEN as _MAX_SEARCH_TERM_LEN,
)
from mediaman.scanner.repository.library_query import (
    _VALID_SORTS as _VALID_SORTS,
)
from mediaman.scanner.repository.library_query import (
    _VALID_TYPES as _VALID_TYPES,
)
from mediaman.scanner.repository.library_query import (
    ALL_SEASON_TYPES as ALL_SEASON_TYPES,
)
from mediaman.scanner.repository.library_query import (
    ANIME_SEASON_TYPES as ANIME_SEASON_TYPES,
)
from mediaman.scanner.repository.library_query import (
    TV_SEASON_TYPES as TV_SEASON_TYPES,
)
from mediaman.scanner.repository.library_query import (
    _days_ago as _days_ago,
)
from mediaman.scanner.repository.library_query import (
    _protection_label as _protection_label,
)
from mediaman.scanner.repository.library_query import (
    _type_css as _type_css,
)
from mediaman.scanner.repository.library_query import (
    fetch_library as fetch_library,
)
from mediaman.services.infra.settings_reader import get_int_setting
from mediaman.web.auth.middleware import resolve_page_session


def fetch_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return counts and stale count for the library stats bar.

    The TV/anime totals use the same grouping definition as
    :func:`fetch_library`'s display_items CTE —
    ``COALESCE(show_rating_key, show_title)``.  When a show has NULL in
    both columns SQLite's ``COUNT(DISTINCT NULL)`` returned 0 while the
    CTE's ``GROUP BY NULL`` collapsed every such row into a single group
    of 1, so the two queries reported different totals on the same data.

    Wrapping the count in a sub-select with ``GROUP BY`` (instead of
    ``COUNT(DISTINCT ...)``) makes the NULL behaviour match: NULL is a
    group in its own right, counted as 1.
    """
    movies = conn.execute(
        "SELECT COUNT(*) AS n FROM media_items WHERE media_type = 'movie'"
    ).fetchone()["n"]

    tv_placeholders = ",".join("?" * len(TV_SEASON_TYPES))
    tv = conn.execute(
        f"SELECT COUNT(*) AS n FROM ("
        f"  SELECT 1 FROM media_items "
        f"  WHERE media_type IN ({tv_placeholders}) "
        f"  GROUP BY COALESCE(show_rating_key, show_title)"
        f")",
        TV_SEASON_TYPES,
    ).fetchone()["n"]

    anime_placeholders = ",".join("?" * len(ANIME_SEASON_TYPES))
    anime = conn.execute(
        f"SELECT COUNT(*) AS n FROM ("
        f"  SELECT 1 FROM media_items "
        f"  WHERE media_type IN ({anime_placeholders}) "
        f"  GROUP BY COALESCE(show_rating_key, show_title)"
        f")",
        ANIME_SEASON_TYPES,
    ).fetchone()["n"]

    min_age = get_int_setting(conn, "min_age_days", default=30)
    inactivity = get_int_setting(conn, "inactivity_days", default=30)

    now = now_utc()
    age_cutoff = (now - timedelta(days=min_age)).isoformat()
    watch_cutoff = (now - timedelta(days=inactivity)).isoformat()

    stale = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM media_items
        WHERE added_at < ?
          AND (last_watched_at IS NULL OR last_watched_at < ?)
    """,
        (age_cutoff, watch_cutoff),
    ).fetchone()["n"]

    total = movies + tv + anime
    total_size_row = conn.execute("SELECT SUM(file_size_bytes) AS n FROM media_items").fetchone()
    total_size = format_bytes(total_size_row["n"] or 0)

    return {
        "movies": movies,
        "tv": tv,
        "anime": anime,
        "stale": stale,
        "stale_min_age": min_age,
        "total": total,
        "total_size": total_size,
    }


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/library", response_class=HTMLResponse)
def library_page(
    request: Request,
    q: str = "",
    type: str = "",
    sort: str = "added_desc",
    page: int = Query(default=1, ge=1, le=100_000),
    per_page: int = Query(default=20, ge=1, le=100),
) -> Response:
    """Render the library page.  Redirects to /login if the session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    # Sort/type silently reset to defaults — these are vocabulary fields and an
    # unknown value is treated as "no filter" rather than an outright error.
    sort = sort if sort in _VALID_SORTS else "added_desc"
    media_type = type if type in _VALID_TYPES else ""

    items, total = fetch_library(
        conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page
    )
    stats = fetch_stats(conn)

    total_pages = max(1, (total + per_page - 1) // per_page)
    page_start = (page - 1) * per_page + 1 if total else 0
    page_end = min(page * per_page, total)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "username": username,
            "nav_active": "library",
            "items": items,
            "stats": stats,
            "q": q,
            "current_type": media_type,
            "current_sort": sort,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "page_start": page_start,
            "page_end": page_end,
        },
    )


# ---------------------------------------------------------------------------
# Compatibility shim — tests that were written against the old package
# structure import from ``mediaman.web.routes.library._query``.  Expose a
# ``_query`` attribute on this module so those imports continue to work
# without needing to migrate every test.
# ---------------------------------------------------------------------------

import types as _types  # noqa: E402

_query = _types.ModuleType("mediaman.web.routes.library._query")
_query._VALID_SORTS = _VALID_SORTS  # type: ignore[attr-defined]
_query._VALID_TYPES = _VALID_TYPES  # type: ignore[attr-defined]
_query._MAX_SEARCH_TERM_LEN = _MAX_SEARCH_TERM_LEN  # type: ignore[attr-defined]
_query.TV_SEASON_TYPES = TV_SEASON_TYPES  # type: ignore[attr-defined]
_query.ANIME_SEASON_TYPES = ANIME_SEASON_TYPES  # type: ignore[attr-defined]
_query.ALL_SEASON_TYPES = ALL_SEASON_TYPES  # type: ignore[attr-defined]
_query._days_ago = _days_ago  # type: ignore[attr-defined]
_query._type_css = _type_css  # type: ignore[attr-defined]
_query._protection_label = _protection_label  # type: ignore[attr-defined]
_query.fetch_library = fetch_library  # type: ignore[attr-defined]
_query.fetch_stats = fetch_stats  # type: ignore[attr-defined]

import sys as _sys  # noqa: E402

_sys.modules.setdefault("mediaman.web.routes.library._query", _query)
