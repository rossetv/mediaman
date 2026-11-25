"""Library page routes (GET /library, a redirect to All titles on Watch).

The JSON API lives in mediaman.web.routes.library_api. Query helpers and shared constants
live in mediaman.web.repository.library_query so library_api can import them without a
peer-route import; this module re-exports them for external importers (tests, app_factory).
"""

from __future__ import annotations

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
from mediaman.web.routes.watch.list_page import address_of

router = APIRouter()


@router.get("/library")
def library_page(request: Request) -> Response:
    """Send the old library page to All titles, keeping the filters there are."""
    resolved = resolve_admin_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    return RedirectResponse(address_of(request.query_params), status_code=302)
