"""Downloads page: unified NZBGet + Radarr/Sonarr queue. Merging, matching and state mapping
live under mediaman.services; the route only authenticates and hands the result to the
template or JSON.
"""

from __future__ import annotations

import sqlite3
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.responses import Response

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.downloads import strip
from mediaman.services.downloads.abandon import abandon_movie, abandon_seasons, abandon_series
from mediaman.services.downloads.download_queue import build_downloads_response
from mediaman.services.downloads.download_queue.items import DownloadsResponse
from mediaman.services.watch_catalog import tmdb_image_url
from mediaman.web.auth.middleware import get_current_admin, get_current_user, resolve_page_session
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.routes.download import drop_arr_cache

router = APIRouter()


class AbandonRequest(BaseModel):
    seasons: list[int] = Field(default_factory=list)


def _ring(data: DownloadsResponse, asked_at: float) -> dict[str, object] | None:
    """What /api/downloads/strip says of what is downloading, its items left out, of what was
    just read: nobody is asked for it."""

    def make(before: strip.Strip) -> strip.Strip:
        items = strip.collect(data, before.items, now_utc())
        return strip.Strip(tuple(items), data["outage"])

    answer = strip.on_page.offer(make, asked_at)
    return {**strip.payload(answer), "items": []} if answer else None


def _page_payload(
    conn: sqlite3.Connection, secret_key: str
) -> tuple[DownloadsResponse, dict[str, object] | None]:
    """The downloads with TMDB posters at mediaman's own address, so a browser here never
    calls TMDB; and what the header's ring is told of them. Copies: completion detection keeps
    the items, with the addresses they came with.
    """

    def shown(item: dict[str, object]) -> dict[str, object]:
        poster = item.get("poster_url") or ""
        return {**item, "poster_url": tmdb_image_url(poster, "w342") or poster}

    asked_at = strip.on_page.now()
    data = build_downloads_response(conn, secret_key)
    page: DownloadsResponse = {
        **data,
        "hero": shown(data["hero"]) if data["hero"] else None,
        "queue": [shown(item) for item in data["queue"]],
        "upcoming": [shown(item) for item in data["upcoming"]],
        "recent": [shown(item) for item in data["recent"]],
    }
    return page, _ring(data, asked_at)


def _lookup_dl_item(
    conn: sqlite3.Connection, secret_key: str, dl_id: str
) -> dict[str, object] | None:
    """Find the queue item with the given dl_id in a freshly built response, never trusting
    stale client-side state. Raises 503 when it is missing while a service is unreachable.
    """
    payload = build_downloads_response(conn, secret_key)
    for bucket in (payload.get("queue", []), payload.get("upcoming", [])):
        for item in bucket:
            if item.get("id") == dl_id:
                return item
    hero = payload.get("hero")
    if hero and hero.get("id") == dl_id:
        return hero
    if outage := payload.get("outage"):
        raise HTTPException(status_code=503, detail=f"{outage} right now. Try again later.")
    return None


def _for_viewer(payload: DownloadsResponse) -> None:
    """Clear what only an admin uses on every item: Abandon, where Radarr and Sonarr are, the
    names of releases, and the ids and dates kept for the server's own bookkeeping."""
    hero = payload["hero"]
    items = [*payload["queue"], *payload["upcoming"]]
    if hero is not None:
        items.append(hero)
    for item in items:
        item.update(abandon_visible=False, arr_link="", arr_source="", arr_id=0)
        item.pop("tmdb_id", None)
        item.pop("queued_since", None)
    for item in [*items, *payload["recent"]]:
        item.update(strip.for_viewer(item))


@router.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    config = request.app.state.config
    data, ring = _page_payload(conn, config.secret_key)
    can_abandon = get_user_role(conn, username) == "admin"
    if not can_abandon:
        _for_viewer(data)
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "downloads.html",
        {
            "nav_active": "downloads",
            "hero": data["hero"],
            "queue": data["queue"],
            "upcoming": data["upcoming"],
            "recent": data["recent"],
            "subtitle": data["subtitle"],
            "outage": data["outage"],
            "strip": ring,
            "can_abandon": can_abandon,
        },
    )


@router.get("/api/downloads")
def downloads_api(request: Request, admin: str = Depends(get_current_user)) -> JSONResponse:
    conn = get_db()
    config = request.app.state.config
    data, ring = _page_payload(conn, config.secret_key)
    if get_user_role(conn, admin) != "admin":
        _for_viewer(data)
    return JSONResponse({**data, "strip": ring})


@router.post("/api/downloads/{dl_id:path}/abandon")
def downloads_abandon(
    request: Request,
    body: AbandonRequest,
    dl_id: str = Path(..., description="dl_id of the item to abandon"),
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Unmonitor a stuck searching item and clear its throttle row.

    Movies ignore seasons; a series needs at least one season number. Returns 404 if the
    dl_id is no longer in the queue (another tab abandoned it, or Radarr just grabbed a
    release) and 400 for a malformed request (empty seasons on a series).
    """
    conn = get_db()
    secret_key = request.app.state.config.secret_key

    item = _lookup_dl_item(conn, secret_key, dl_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in queue")

    arr_id_raw = item.get("arr_id")
    arr_id: int = int(arr_id_raw) if isinstance(arr_id_raw, int) else 0
    if arr_id == 0:
        raise HTTPException(status_code=400, detail="Item has no upstream arr id")

    if item.get("kind") == "movie":
        result = abandon_movie(conn, secret_key, arr_id=arr_id, dl_id=dl_id)
    elif item.get("state") == "upcoming":
        # "Coming soon" series have no stuck-season rows; the user wants to
        # stop tracking the whole show, so unmonitor every monitored season.
        result = abandon_series(conn, secret_key, series_id=arr_id, dl_id=dl_id)
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

    # The emailed page must not offer again what was just unmonitored; a failed abandon may
    # have changed the Arr too.
    drop_arr_cache()
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
