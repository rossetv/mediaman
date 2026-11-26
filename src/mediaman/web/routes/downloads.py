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
from mediaman.services import watch_catalog as catalog
from mediaman.services.arr.fetcher._base import is_film_id
from mediaman.services.downloads import strip
from mediaman.services.downloads.abandon import abandon_movie, abandon_seasons, abandon_series
from mediaman.services.downloads.download_queue import (
    VIEWER_OUTAGE,
    build_downloads_response,
    downloads_subtitle,
)
from mediaman.services.downloads.download_queue.items import DownloadsResponse
from mediaman.services.watch_catalog import tmdb_image_url
from mediaman.web.auth.middleware import get_current_admin, get_current_user, resolve_page_session
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.responses import respond_failure
from mediaman.web.routes.download import drop_arr_cache
from mediaman.web.routes.downloads_strip import (
    held_poster,
    library_of,
    library_title,
    mark_playable,
)
from mediaman.web.routes.poster.title_art import title_art

router = APIRouter()


class AbandonRequest(BaseModel):
    seasons: list[int] = Field(default_factory=list)
    arr_id: int | None = None


def _ring(
    data: DownloadsResponse, asked_at: float, index: catalog.LibraryIndex, *, admin: bool
) -> dict[str, object] | None:
    """What /api/downloads/strip says of what is downloading, its items left out, of what was
    just read: nobody is asked for it."""

    def make(before: strip.Strip) -> strip.Strip:
        items = mark_playable(strip.collect(data, before.items, now_utc()), index)
        return strip.Strip(tuple(items), data["outage"])

    answer = strip.on_page.offer(make, asked_at)
    return {**strip.payload(answer, admin=admin), "items": []} if answer else None


def _tmdb_id_of(item: dict[str, object]) -> int | None:
    """The TMDB id of a download: its own, or the one in a film's id."""
    dl_id, title, tmdb_id = str(item["id"]), str(item["title"]), item.get("tmdb_id")
    if isinstance(tmdb_id, int) and tmdb_id:
        return tmdb_id
    return int(dl_id.rpartition("#")[2]) if is_film_id(dl_id, title) else None


def _library_ref(item: dict[str, object], index: catalog.LibraryIndex) -> str | None:
    kind = "movie" if item.get("media_type") == "movie" else "tv"
    return library_title(index, kind, _tmdb_id_of(item), str(item["title"]))


def _in_library(item: dict[str, object], index: catalog.LibraryIndex) -> bool:
    """Whether the title of a recent download is there to play."""
    return _library_ref(item, index) is not None


def _poster(item: dict[str, object], index: catalog.LibraryIndex) -> str:
    """Its poster at mediaman's own address: TMDB's, the one TMDB told of its title before, or
    the library's; "" when the app knows none."""
    own = tmdb_image_url(item.get("poster_url") or "", "w342")
    if own:
        return own
    tmdb_id = _tmdb_id_of(item)
    kind = "movie" if item.get("media_type") == "movie" else "tv"
    known = held_poster(kind, tmdb_id) if tmdb_id else ""
    ref = None if known else _library_ref(item, index)
    return known or (title_art(ref.partition("-")[2])["poster_url"] if ref else "")


def _page_payload(
    conn: sqlite3.Connection, secret_key: str, *, admin: bool
) -> tuple[DownloadsResponse, dict[str, object] | None]:
    """The downloads with posters at mediaman's own address, so a browser here never calls
    TMDB or another service, and films of one title by their labels; and what the header's
    ring is told of them. A recent download the library does not hold yet was only added. A
    viewer is told of a service that does not answer only that downloads are not available.
    Copies: completion detection keeps the items as they came.
    """
    asked_at = strip.on_page.now()
    data = build_downloads_response(conn, secret_key)
    index = library_of(conn, secret_key)

    def shown(item: dict[str, object]) -> dict[str, object]:
        title = item.get("label") or item["title"]
        return {**item, "title": title, "poster_url": _poster(item, index)}

    page: DownloadsResponse = {
        **data,
        "hero": shown(data["hero"]) if data["hero"] else None,
        "queue": [shown(item) for item in data["queue"]],
        "upcoming": [shown(item) for item in data["upcoming"]],
        "recent": [shown(item) for item in data["recent"]],
    }
    for item in page["recent"]:
        if not _in_library(item, index):
            item["readiness"] = "unknown"
    if not admin and data["outage"]:
        page["outage"], page["unreachable"], page["refused"] = VIEWER_OUTAGE, [], []
        # In the words downloads.js keeps the line in, so the first render and the poll agree.
        rows, coming = (1 if page["hero"] else 0) + len(page["queue"]), len(page["upcoming"])
        soon = f"{coming} coming soon" if coming else ""
        page["subtitle"] = (
            f"{downloads_subtitle(rows, coming)} · some may be missing" if rows else soon
        )
    return page, _ring(data, asked_at, index, admin=admin)


def _former_id(item: dict[str, object]) -> str | None:
    dl_id, title = str(item["id"]), str(item["title"])
    return f"radarr:{title}" if is_film_id(dl_id, title) else None


def _is_shown_as(item: dict[str, object], dl_id: str) -> bool:
    """Whether *item* is the title a page showed as *dl_id*: a film by its TMDB id, which stays
    with it when it is renamed, and anything else by its id. Radarr and Sonarr hand a number
    out again once reinstalled, so that alone does not tell."""
    item_id = str(item["id"])
    _, has_number, tmdb_id = dl_id.rpartition("#")
    if dl_id.startswith("radarr:") and has_number and tmdb_id.isdecimal():
        return is_film_id(item_id, str(item["title"])) and item_id.endswith(f"#{tmdb_id}")
    return item_id == dl_id


def _lookup_dl_item(
    conn: sqlite3.Connection, secret_key: str, dl_id: str, arr_id: int | None = None
) -> dict[str, object] | None:
    """Find the queue item a page showed as *dl_id* in a freshly built response, never trusting
    stale client-side state: with *arr_id*, the one of that number that is still the same title.
    Without an arr_id it is the one with the dl_id, which of a film may be the one it had when
    films had none of their own. Raises 409 when that is more than one title, and 503 when none
    is while a service is unreachable.
    """
    payload = build_downloads_response(conn, secret_key, steady=False)
    items = [*payload.get("queue", []), *payload.get("upcoming", [])]
    if hero := payload.get("hero"):
        items.append(hero)
    if arr_id:
        found = [i for i in items if i.get("arr_id") == arr_id and _is_shown_as(i, dl_id)]
    else:
        found = [i for i in items if dl_id in (i["id"], _former_id(i))]
    if len({item.get("arr_id") for item in found}) > 1:
        raise HTTPException(
            status_code=409,
            detail="More than one title has this name. Reload the page and try again.",
        )
    if found:
        return found[0]
    if (outage := payload.get("outage")) and payload.get("refused"):
        raise HTTPException(status_code=503, detail=f"{outage}. Check its details in Settings.")
    if outage:
        raise HTTPException(status_code=503, detail=f"{outage} right now. Try again later.")
    return None


def _for_viewer(payload: DownloadsResponse) -> None:
    """Clear what only an admin uses on every item: Abandon, where Radarr and Sonarr are and
    what they say is wrong, the names of releases, and the ids and dates kept for the server's
    own bookkeeping."""
    hero = payload["hero"]
    items = [*payload["queue"], *payload["upcoming"]]
    if hero is not None:
        items.append(hero)
    for item in items:
        item.update(
            abandon_visible=False,
            arr_link="",
            arr_source="",
            arr_id=0,
            attention="",
            attention_hint="",
        )
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
    can_abandon = get_user_role(conn, username) == "admin"
    data, ring = _page_payload(conn, config.secret_key, admin=can_abandon)
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
            "refused": data["refused"],
            "downloads_set_up": data["downloads_set_up"],
            "strip": ring,
            "can_abandon": can_abandon,
        },
    )


@router.get("/api/downloads")
def downloads_api(request: Request, admin: str = Depends(get_current_user)) -> JSONResponse:
    conn = get_db()
    config = request.app.state.config
    is_admin = get_user_role(conn, admin) == "admin"
    data, ring = _page_payload(conn, config.secret_key, admin=is_admin)
    if not is_admin:
        _for_viewer(data)
    return JSONResponse({**data, "strip": ring})


@router.post("/api/downloads/{dl_id:path}/abandon")
def downloads_abandon(
    request: Request,
    body: AbandonRequest,
    dl_id: str = Path(..., description="dl_id of the item to abandon"),
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Unmonitor a stuck searching item and clear its throttle row. A page sends the arr_id of
    the item it showed; one opened before it did sends none.

    Movies ignore seasons; a series needs at least one season number. Returns 404 if the
    item is no longer in the queue (another tab abandoned it, or Radarr just grabbed a
    release), 409 if it names more than one title and 400 for a malformed request (empty
    seasons on a series).
    """
    try:
        return _abandon(request, body, dl_id)
    except HTTPException as exc:
        words = str(exc.detail)
        return respond_failure(words, status=exc.status_code, detail=words)


def _abandon(request: Request, body: AbandonRequest, dl_id: str) -> JSONResponse:
    conn = get_db()
    secret_key = request.app.state.config.secret_key

    item = _lookup_dl_item(conn, secret_key, dl_id, body.arr_id)
    if item is None:
        raise HTTPException(status_code=404, detail="It's no longer in the queue.")

    # What it is known by now, which a page that sent its arr_id may not have had.
    dl_id = str(item.get("id") or dl_id)
    arr_id_raw = item.get("arr_id")
    arr_id: int = int(arr_id_raw) if isinstance(arr_id_raw, int) else 0
    if arr_id == 0:
        raise HTTPException(status_code=400, detail="Radarr and Sonarr don't track it.")

    if item.get("kind") == "movie":
        result = abandon_movie(conn, secret_key, arr_id=arr_id, dl_id=dl_id)
    elif item.get("state") == "upcoming":
        # "Coming soon" series have no stuck-season rows; the user wants to
        # stop tracking the whole show, so unmonitor every monitored season.
        result = abandon_series(conn, secret_key, series_id=arr_id, dl_id=dl_id)
    else:
        if not body.seasons:
            raise HTTPException(status_code=400, detail="Choose at least one season.")
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
    abandoned = {"kind": result.kind, "succeeded": result.succeeded, "failed": result.failed}
    if result.failed:
        service = "Radarr" if result.kind == "movie" else "Sonarr"
        done = "stop tracking" if item.get("state") == "upcoming" else "abandon"
        words = f"{service} didn't {done} it. Try again."
        return respond_failure(words, status=502, abandoned=abandoned)
    return JSONResponse({"ok": True, "abandoned": abandoned})
