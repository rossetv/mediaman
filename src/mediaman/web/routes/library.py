"""Library page routes (GET /library).

The JSON API lives in mediaman.web.routes.library_api. Query helpers and shared constants
live in mediaman.web.repository.library_query so library_api can import them without a
peer-route import; this module re-exports them for external importers (tests, app_factory).
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.web.auth.middleware import resolve_admin_page_session
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
    days_ago as days_ago,
)
from mediaman.web.repository.library_query import (
    fetch_library as fetch_library,
)
from mediaman.web.repository.library_query import (
    fetch_stats as fetch_stats,
)
from mediaman.web.repository.library_query import (
    protection_label as protection_label,
)
from mediaman.web.repository.library_query import (
    type_css as type_css,
)

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
    resolved = resolve_admin_page_session(request)
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
    if page > total_pages:
        # A stale link or a reload after removing the last row lands past the end.
        page = total_pages
        items, total = fetch_library(
            conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page
        )
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
            "plex_configured": plex_is_configured(conn),
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
