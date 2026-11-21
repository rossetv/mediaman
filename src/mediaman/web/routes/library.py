"""Library page routes (GET /library).

The JSON API lives in mediaman.web.routes.library_api. Query helpers and shared constants
live in mediaman.web.repository.library_query so library_api can import them without a
peer-route import; this module re-exports them for external importers (tests, app_factory).
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.format import format_bytes
from mediaman.core.time import now_utc
from mediaman.services.infra import get_int_setting
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.repository.library_query import (
    ALL_SEASON_TYPES as ALL_SEASON_TYPES,
)
from mediaman.web.repository.library_query import (
    ANIME_SEASON_TYPES as ANIME_SEASON_TYPES,
)
from mediaman.web.repository.library_query import (
    MAX_SEARCH_TERM_LEN as MAX_SEARCH_TERM_LEN,
)
from mediaman.web.repository.library_query import (
    TV_SEASON_TYPES as TV_SEASON_TYPES,
)
from mediaman.web.repository.library_query import (
    VALID_SORTS as VALID_SORTS,
)
from mediaman.web.repository.library_query import (
    VALID_TYPES as VALID_TYPES,
)
from mediaman.web.repository.library_query import (
    count_anime_shows,
    count_movies,
    count_stale,
    count_tv_shows,
    sum_total_size_bytes,
)
from mediaman.web.repository.library_query import (
    days_ago as days_ago,
)
from mediaman.web.repository.library_query import (
    fetch_library as fetch_library,
)
from mediaman.web.repository.library_query import (
    protection_label as protection_label,
)
from mediaman.web.repository.library_query import (
    type_css as type_css,
)


def fetch_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return counts and stale count for the library stats bar.

    TV/anime totals group like fetch_library's display_items CTE, by
    COALESCE(show_rating_key, show_title). COUNT(DISTINCT NULL) counts 0 while the CTE's
    GROUP BY NULL collapses such rows into one group, so the count wraps a GROUP BY
    sub-select to make NULL count as 1.
    """
    movies = count_movies(conn)
    tv = count_tv_shows(conn)
    anime = count_anime_shows(conn)

    min_age = get_int_setting(conn, "min_age_days", default=30)
    inactivity = get_int_setting(conn, "inactivity_days", default=30)

    now = now_utc()
    age_cutoff = (now - timedelta(days=min_age)).isoformat()
    watch_cutoff = (now - timedelta(days=inactivity)).isoformat()

    stale = count_stale(conn, age_cutoff=age_cutoff, watch_cutoff=watch_cutoff)

    total = movies + tv + anime
    total_size = format_bytes(sum_total_size_bytes(conn))

    return {
        "movies": movies,
        "tv": tv,
        "anime": anime,
        "stale": stale,
        "stale_min_age": min_age,
        "total": total,
        "total_size": total_size,
    }


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
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    # Sort/type silently reset to defaults — these are vocabulary fields and an
    # unknown value is treated as "no filter" rather than an outright error.
    sort = sort if sort in VALID_SORTS else "added_desc"
    media_type = type if type in VALID_TYPES else ""

    items, total = fetch_library(
        conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page
    )
    stats = fetch_stats(conn)

    total_pages = max(1, (total + per_page - 1) // per_page)
    page_start = (page - 1) * per_page + 1 if total else 0
    page_end = min(page * per_page, total)

    templates = cast(Jinja2Templates, request.app.state.templates)
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
