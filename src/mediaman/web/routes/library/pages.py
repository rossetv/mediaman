"""Library page route."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import resolve_page_session

from ._query import _VALID_SORTS, _VALID_TYPES, fetch_library, fetch_stats

router = APIRouter()


@router.get("/library", response_class=HTMLResponse)
def library_page(
    request: Request,
    q: str = "",
    type: str = "",
    sort: str = "added_desc",
    # Pagination bounds enforced at the input layer (finding 17).
    # Pydantic ``ge=1`` / ``le=100`` rejects malformed values with HTTP
    # 422 instead of silently rewriting them, so the user sees the bad
    # request rather than getting an unexpected page.
    page: int = Query(default=1, ge=1, le=100_000),
    per_page: int = Query(default=20, ge=1, le=100),
) -> Response:
    """Render the library page. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    # Sort/type still tolerated as silent reset to defaults — these are
    # vocabulary fields and an unknown value is treated as "no filter"
    # rather than an outright error.
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
