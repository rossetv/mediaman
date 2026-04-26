"""Downloads page — unified NZBGet + Radarr/Sonarr queue.

This module is intentionally thin: all the merging, matching, state
mapping, completion detection, and recent-downloads persistence lives
under ``mediaman.services``. The route layer just authenticates the
request and hands the response dict to the template or JSON serialiser.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.db import get_db
from mediaman.services.downloads.download_queue import build_downloads_response

router = APIRouter()


@router.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request) -> Response:
    """Render the unified downloads page."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    _username, conn = resolved

    config = request.app.state.config
    data = build_downloads_response(conn, config.secret_key)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "downloads.html",
        {
            "nav_active": "downloads",
            "hero": data["hero"],
            "queue": data["queue"],
            "upcoming": data["upcoming"],
            "recent": data["recent"],
        },
    )


@router.get("/api/downloads")
def downloads_api(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return the simplified download queue as JSON."""
    conn = get_db()
    config = request.app.state.config
    return JSONResponse(build_downloads_response(conn, config.secret_key))
