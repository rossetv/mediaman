"""The admin list view of the library on the Watch page.

``GET /watch/list`` answers the list as an HTML fragment for the page to swap
in; ``/watch?view=list`` renders the same fragment inline through
`render_list`. Both are for admins only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.time import parse_iso_strict_utc
from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.web.auth.middleware import resolve_admin_page_session
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.library_query import (
    MAX_SEARCH_TERM_LEN,
    VALID_SORTS,
    VALID_TYPES,
    fetch_library,
    fetch_stats,
)

router = APIRouter()

DEFAULT_SORT = "added_desc"
DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100
MAX_PAGE = 100_000

SORT_OPTIONS = (
    ("added_desc", "Recently added"),
    ("name_asc", "Title (A–Z)"),
    ("name_desc", "Title (Z–A)"),
    ("size_desc", "Size (largest)"),
    ("size_asc", "Size (smallest)"),
    ("watched_desc", "Last watched by anyone (recent)"),
    ("watched_asc", "Last watched by anyone (oldest)"),
    ("deletion_asc", "Deletion (soonest)"),
    ("deletion_desc", "Deletion (latest)"),
)

# A deletion this close shows in the warning colour.
SOON_DAYS = 14


def _int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        number = int(value) if value is not None else default
    except ValueError:
        return default
    return min(max(number, low), high)


def _deletion_label(iso: str | None) -> str:
    when = parse_iso_strict_utc(iso)
    return f"{when.day} {when:%b}" if when else ""


def list_context(
    conn: sqlite3.Connection, username: str, params: Mapping[str, str]
) -> dict[str, Any]:
    """The rows, stats and paging of the list for *params*; unknown values fall back to defaults.

    A row's ``watched`` is *username*'s own mark, apart from the cleanup's last watched time.
    """
    q = (params.get("q") or "").strip()[:MAX_SEARCH_TERM_LEN]
    media_type = params.get("type") or ""
    media_type = media_type if media_type in VALID_TYPES else ""
    sort = params.get("sort") or DEFAULT_SORT
    sort = sort if sort in VALID_SORTS else DEFAULT_SORT
    page = _int(params.get("page"), 1, 1, MAX_PAGE)
    per_page = _int(params.get("per_page"), DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)

    items, total = fetch_library(
        conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        # A stale link, or the last row of the last page just went.
        page = total_pages
        items, total = fetch_library(
            conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page
        )
    seen = lib.seen_refs(conn, username, lib.scanned_library_ids(conn))
    for item in items:
        item["watched"] = item["ref"] in seen
        item["deletion_label"] = _deletion_label(cast(str | None, item["deletion_at"]))

    def href(**changes: object) -> str:
        """The list's address with *changes* applied, leaving out values at their default."""
        state: dict[str, object] = {
            "q": q,
            "type": media_type,
            "sort": sort,
            "page": page,
            "per_page": per_page,
        }
        state.update(changes)
        defaults = {
            "q": "",
            "type": "",
            "sort": DEFAULT_SORT,
            "page": 1,
            "per_page": DEFAULT_PER_PAGE,
        }
        query = [("view", "list")] + [
            (key, str(value)) for key, value in state.items() if value != defaults[key]
        ]
        return "/watch?" + urlencode(query)

    return {
        "items": items,
        "stats": fetch_stats(conn),
        "plex_configured": plex_is_configured(conn),
        "q": q,
        "current_type": media_type,
        "current_sort": sort,
        "sort_options": SORT_OPTIONS,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "page_start": (page - 1) * per_page + 1 if total else 0,
        "page_end": min(page * per_page, total),
        "soon_days": SOON_DAYS,
        "href": href,
    }


@router.get("/watch/list", response_class=HTMLResponse)
def watch_list(request: Request) -> Response:
    resolved = resolve_admin_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    templates = cast(Jinja2Templates, request.app.state.templates)
    response = templates.TemplateResponse(
        request, "_watch_list.html", list_context(conn, username, request.query_params)
    )
    response.headers["Cache-Control"] = "no-store"
    return response
