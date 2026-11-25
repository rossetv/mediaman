"""The old list view's addresses, which lead to All titles."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from mediaman.web.auth.middleware import resolve_admin_page_session

router = APIRouter()

_TYPES = {"movie": "movies", "tv": "tv", "anime": "anime", "kept": "kept", "stale": "stale"}
_SORTS = {
    "name_asc": "az",
    "name_desc": "az",
    "size_desc": "size",
    "size_asc": "small",
    "watched_desc": "seen",
    "watched_asc": "stalest",
    "deletion_asc": "leaving",
    "deletion_desc": "leaving",
}


def address_of(params: Mapping[str, str], *, admin: bool = True) -> str:
    """Where a list view of *params* is now: the search when it searched, else All titles with the
    filter and sort of it that there are, for an *admin* or a viewer."""
    q = (params.get("q") or "").strip()[:200]
    if q:
        return f"/watch?{urlencode({'q': q})}"
    to = {"all": "library"}
    kind = _TYPES.get(params.get("type") or "")
    if kind and (admin or kind not in ("kept", "stale")):
        to["type"] = kind
    sort = _SORTS.get(params.get("sort") or "")
    if sort and (admin or sort == "az"):
        to["sort"] = sort
    return f"/watch?{urlencode(to)}"


@router.get("/watch/list")
def watch_list(request: Request) -> Response:
    resolved = resolve_admin_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    return RedirectResponse(address_of(request.query_params), status_code=302)
