"""Downloads page — unified NZBGet + Radarr/Sonarr queue.

This module is intentionally thin: all the merging, matching, state
mapping, completion detection, and recent-downloads persistence lives
under ``mediaman.services``. The route layer just authenticates the
request and hands the response dict to the template or JSON serialiser.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.responses import Response

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.db import get_db
from mediaman.services.downloads.abandon import abandon_movie, abandon_seasons
from mediaman.services.downloads.download_queue import build_downloads_response

router = APIRouter()


class AbandonRequest(BaseModel):
    seasons: list[int] = Field(default_factory=list)


def _lookup_dl_item(conn: sqlite3.Connection, secret_key: str, dl_id: str) -> dict | None:
    """Find the queue item with the given dl_id.

    Lookup happens against a freshly-built response so we don't trust
    any stale client-side state.
    """
    payload = build_downloads_response(conn, secret_key)
    for bucket in (payload.get("queue", []), payload.get("upcoming", [])):
        for item in bucket:
            if item.get("id") == dl_id:
                return item
    hero = payload.get("hero")
    if hero and hero.get("id") == dl_id:
        return hero
    return None


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


@router.post("/api/downloads/{dl_id:path}/abandon")
def downloads_abandon(
    request: Request,
    body: AbandonRequest,
    dl_id: str = Path(..., description="dl_id of the item to abandon"),
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Unmonitor a stuck searching item and clear its throttle row.

    Movies: ``seasons`` is ignored; the whole movie is unmonitored.
    Series: ``seasons`` MUST contain at least one season number.

    Returns 404 if the dl_id is no longer in the queue (e.g. another
    tab already abandoned it, or Radarr just grabbed a release), and
    400 if the request is malformed (empty seasons on a series).
    """
    conn = get_db()
    secret_key = request.app.state.config.secret_key

    item = _lookup_dl_item(conn, secret_key, dl_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in queue")

    arr_id = item.get("arr_id") or 0
    if arr_id == 0:
        raise HTTPException(status_code=400, detail="Item has no upstream arr id")

    if item.get("kind") == "movie":
        result = abandon_movie(conn, secret_key, arr_id=arr_id, dl_id=dl_id)
    else:
        if not body.seasons:
            raise HTTPException(
                status_code=400,
                detail="Series abandon requires at least one season",
            )
        result = abandon_seasons(
            conn,
            secret_key,
            series_id=arr_id,
            season_numbers=body.seasons,
            dl_id=dl_id,
        )

    refreshed = build_downloads_response(conn, secret_key)
    return JSONResponse(
        {
            "ok": not result.failed,
            "abandoned": {
                "kind": result.kind,
                "succeeded": result.succeeded,
                "failed": result.failed,
            },
            "queue": refreshed,
        },
        status_code=200 if not result.failed else 502,
    )
