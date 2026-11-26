"""What is downloading, as the Watch page's row and the header's ring show it."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.downloads import strip
from mediaman.services.downloads.download_queue import build_downloads_response
from mediaman.services.downloads.strip import Answer, Strip, StripItem
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.seen_titles import person_marks
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.poster.title_art import title_art
from mediaman.web.routes.watch.card_marks import card_marks, marked

logger = logging.getLogger(__name__)

router = APIRouter()

# A title's ref has at most this many digits.
_MAX_TMDB_ID = 9_999_999_999


@router.get("/api/downloads/strip")
def api_downloads_strip(
    request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    try:
        answer = strip.shared.get(lambda before: _make(conn, secret_key, before))
    except strip.Unavailable:
        return respond_err("busy", status=503)
    return respond_ok(payload_for(conn, secret_key, username, answer))


def payload_for(
    conn: sqlite3.Connection, secret_key: str, username: str, answer: Answer
) -> dict[str, Any]:
    """What a page is told, with *username*'s own marks of what is ready, for its card in
    Recently added."""
    shown = strip.payload(answer, admin=get_user_role(conn, username) == "admin")
    ready = [i for i in shown["items"] if i["state"] == "ready" and i["tmdb_ref"]]
    if ready:
        marks = card_marks(person_marks(conn, username), TmdbClient.from_db(conn, secret_key))
        seen = lib.seen_refs(conn, username, lib.scanned_library_ids(conn))
        for item in ready:
            held = None if item["ref"].startswith("tmdb-") else item["ref"]
            item.update(lib.mark_fields(held, seen))
            card = marked({**item, "ref": item["tmdb_ref"], "library_ref": held}, marks)
            item.update(level=card["level"], watched=card["watched"])
    return shown


def _make(conn: sqlite3.Connection, secret_key: str, before: Strip) -> Strip:
    data = build_downloads_response(conn, secret_key)
    items = strip.collect(data, before.items, now_utc())
    return Strip(
        tuple(_with_titles(conn, secret_key, items)), data["outage"], refused=bool(data["refused"])
    )


def _with_titles(
    conn: sqlite3.Connection, secret_key: str, items: list[StripItem]
) -> list[StripItem]:
    """*items* with the titles they open and their pictures; TMDB's at mediaman's own address."""
    if not items:
        return []
    index = catalog.library_index(
        conn,
        TmdbClient.from_db(conn, secret_key),
        lib.scanned_library_ids(conn),
        lib.library_movies,
        lib.library_shows,
    )
    arr = catalog.arr_view(conn, secret_key)
    return [
        _with_title(item, index, arr, shown=n < strip.MAX_ITEMS) for n, item in enumerate(items)
    ]


def _with_title(
    item: StripItem, index: catalog.LibraryIndex, arr: catalog.ArrView, *, shown: bool
) -> StripItem:
    poster = catalog.tmdb_image_url(item.poster_url, "w342") or ""
    if item.tmdb_id is None or item.tmdb_id > _MAX_TMDB_ID:
        return replace(item, poster_url=poster, line=_ready_line(item, None))
    kind = "movie" if item.kind == "movie" else "tv"
    held = index.library_ref(kind, item.tmdb_id, item.title, arr)
    ref = held or f"tmdb-{kind}-{item.tmdb_id}"
    art = _fanart(item, arr)
    if shown and not (poster and art):
        known = _held(kind, item.tmdb_id)
        poster = poster or known.get("poster_url") or ""
        art = art or known.get("backdrop_url") or ""
    art = art or item.art_url
    if held is not None:
        own = title_art(held.partition("-")[2])
        poster = poster or own["poster_url"]
        art = art or own["backdrop_url"]
    return replace(
        item,
        ref=ref,
        href=catalog.library_href(held) or f"/watch#title={ref}",
        poster_url=poster,
        art_url=art,
        line=_ready_line(item, held),
    )


def _ready_line(item: StripItem, held: str | None) -> str:
    """What a download that is done says: that it was added, until the library holds the
    title it plays."""
    return "Added" if item.state == "ready" and held is None else item.line


def _held(kind: str, tmdb_id: int) -> dict[str, Any]:
    """What TMDB told of a title before: nobody waits on it here."""
    try:
        return catalog.held_details(kind, tmdb_id) or {}
    except Exception:  # whatever its last ask failed with, kept for a while
        logger.warning("Failed to read what is held of %s %s", kind, tmdb_id, exc_info=True)
        return {}


def _fanart(item: StripItem, arr: catalog.ArrView) -> str:
    """Radarr's wide picture of a film, which is TMDB's; Sonarr's are not."""
    if item.kind != "movie" or item.tmdb_id is None:
        return ""
    for image in arr.caches["radarr_movies"].get(item.tmdb_id, {}).get("images") or []:
        if image.get("coverType") == "fanart":
            return catalog.tmdb_image_url(image.get("remoteUrl"), "w1280") or ""
    return ""
