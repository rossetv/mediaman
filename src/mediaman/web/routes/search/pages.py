"""Search page route."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import resolve_page_session

router = APIRouter()


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "search.html", {
        "username": username,
        "nav_active": "search",
    })
