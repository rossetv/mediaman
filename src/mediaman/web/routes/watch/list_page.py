"""The admin list view of the library on the Watch page.

``GET /watch/list`` answers the list as an HTML fragment for the page to swap
in; ``/watch?view=list`` renders the same fragment inline through
`render_list`. Both are for admins only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.web.auth.middleware import resolve_admin_page_session

router = APIRouter()


def render_list(request: Request, conn: sqlite3.Connection, params: Mapping[str, str]) -> str:
    """The list view's markup for the filters in *params* (q, type, sort, page, per_page)."""
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.get_template("_watch_list.html").render(request=request)


@router.get("/watch/list", response_class=HTMLResponse)
def watch_list(request: Request) -> Response:
    resolved = resolve_admin_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    _, conn = resolved
    return HTMLResponse(render_list(request, conn, request.query_params))
