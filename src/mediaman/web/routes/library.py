"""Library page routes (GET /library, a redirect to the Watch list view).

The JSON API lives in mediaman.web.routes.library_api. Query helpers and shared constants
live in mediaman.web.repository.library_query so library_api can import them without a
peer-route import; this module re-exports them for external importers (tests, app_factory).
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

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


@router.get("/library")
def library_page(
    request: Request,
    q: str = "",
    type: str = "",
    sort: str = "",
    page: str = "",
    per_page: str = "",
) -> Response:
    """Send the old library page to the Watch list view, keeping the filters that are valid."""
    resolved = resolve_admin_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    params = {"view": "list"}
    q = q.strip()[:MAX_SEARCH_TERM_LEN]
    if q:
        params["q"] = q
    if type in VALID_TYPES:
        params["type"] = type
    if sort in VALID_SORTS:
        params["sort"] = sort
    if page.isascii() and page.isdecimal() and 1 <= int(page) <= 100_000:
        params["page"] = page
    if per_page.isascii() and per_page.isdecimal() and 1 <= int(per_page) <= 100:
        params["per_page"] = per_page
    return RedirectResponse(f"/watch?{urlencode(params)}", status_code=302)
