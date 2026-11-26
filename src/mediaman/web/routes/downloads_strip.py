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
    return Strip(tuple(_with_titles(conn, secret_key, items)), data["outage"])


def _with_titles(
    conn: sqlite3.Connection, secret_key: str, items: list[StripItem]
) -> list[StripItem]:
    """*items* with the titles they open and their pictures; TMDB's at mediaman's own address."""
    if not items:
        return []
    index = library_of(conn, secret_key)
    arr = catalog.arr_view(conn, secret_key)
    titled = [
        _with_title(item, index, arr, shown=n < strip.MAX_ITEMS) for n, item in enumerate(items)
    ]
    return mark_playable(titled, index)


def library_of(conn: sqlite3.Connection, secret_key: str) -> catalog.LibraryIndex:
    """The index of the scanned libraries' titles, as held."""
    return catalog.library_index(
        conn,
        TmdbClient.from_db(conn, secret_key),
        lib.scanned_library_ids(conn),
        lib.library_movies,
        lib.library_shows,
    )


def mark_playable(items: list[StripItem], index: catalog.LibraryIndex) -> list[StripItem]:
    """*items*, of which one that is done but that the library does not hold yet says it was
    added, and is not offered to play."""
    return [
        replace(item, line=strip.WAITING_LINE, playable=False)
        if item.state == "ready"
        and library_title(index, item.kind, item.tmdb_id, item.title) is None
        else item
        for item in items
    ]


def _with_title(
    item: StripItem, index: catalog.LibraryIndex, arr: catalog.ArrView, *, shown: bool
) -> StripItem:
    poster = catalog.tmdb_image_url(item.poster_url, "w342") or ""
    kind = "movie" if item.kind == "movie" else "tv"
    if item.tmdb_id is None or item.tmdb_id > _MAX_TMDB_ID:
        poster = poster or _library_poster(index, kind, None, item.title)
        return replace(item, poster_url=poster)
    held = index.library_ref(kind, item.tmdb_id, item.title, arr) or library_title(
        index, kind, item.tmdb_id, item.title
    )
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
    poster = poster or _library_poster(index, kind, item.tmdb_id, item.title)
    return replace(
        item,
        ref=ref,
        href=catalog.library_href(held) or f"/#title={ref}",
        poster_url=poster,
        art_url=art,
    )


def library_title(
    index: catalog.LibraryIndex,
    kind: str,
    tmdb_id: int | None,
    title: str,
    year: int | None = None,
) -> str | None:
    """The library title ("movie" or "tv") by its TMDB id where that is known, a show's else by
    its title; None when it is not held. A film's else is the library film of its title, of its
    *year* where both are known, and with no TMDB id of its own where the film's is known."""
    name = catalog.normalise_title(title)
    if kind != "movie":
        show = index.shows_by_tmdb.get(tmdb_id) if tmdb_id else None
        return show or index.shows_by_title.get(name)
    found = index.movies_by_tmdb.get(tmdb_id) if tmdb_id else None
    if found is not None:
        return found
    held = index.movies_by_title.get(name)
    if held is None or (tmdb_id and held in index.movies_by_tmdb.values()):
        return None
    held_year = index.year_of.get(held)
    # One apart is one film, dated by two of its releases.
    return None if year and held_year and abs(year - held_year) > 1 else held


def _library_poster(index: catalog.LibraryIndex, kind: str, tmdb_id: int | None, title: str) -> str:
    ref = library_title(index, kind, tmdb_id, title)
    return title_art(ref.partition("-")[2])["poster_url"] if ref else ""


def held_poster(kind: str, tmdb_id: int) -> str:
    """The poster TMDB told of a title before, at mediaman's own address; "" when none."""
    return str(_held(kind, tmdb_id).get("poster_url") or "")


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
