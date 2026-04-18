"""Downloads page — unified NZBGet + Radarr/Sonarr queue.

This module is intentionally thin: all the merging, matching, state
mapping, completion detection, and recent-downloads persistence lives
under ``mediaman.services``. The route layer just authenticates the
request and hands the response dict to the template or JSON serialiser.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mediaman.auth.middleware import get_current_admin
from mediaman.auth.session import validate_session
from mediaman.db import get_db
from mediaman.services.download_queue import build_downloads_response

router = APIRouter()


@router.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request):
    """Render the unified downloads page."""
    token = request.cookies.get("session_token")
    conn = get_db()
    if not token or not validate_session(conn, token):
        return RedirectResponse("/login", status_code=302)

    data = build_downloads_response(conn)
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
def downloads_api(admin: str = Depends(get_current_admin)):
    """Return the simplified download queue as JSON."""
    conn = get_db()
    return JSONResponse(build_downloads_response(conn))
