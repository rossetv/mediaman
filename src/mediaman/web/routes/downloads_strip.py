"""What is downloading, as the Watch page's row and the header's ring show it."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.arr.finding_copy import fixing_words
from mediaman.services.arr.import_retry import stopped_words
from mediaman.services.downloads import strip
from mediaman.services.downloads.download_queue import build_downloads_response
from mediaman.services.downloads.download_queue.items import DownloadsResponse
from mediaman.services.downloads.strip import Answer, Strip, StripItem
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.seen_titles import person_marks
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.poster.title_art import title_art
from mediaman.web.routes.watch.card_marks import card_marks, marked, scored

logger = logging.getLogger(__name__)

router = APIRouter()

# A title's ref has at most this many digits.
_MAX_TMDB_ID = 9_999_999_999


def is_tmdb_id(tmdb_id: int | None) -> bool:
    """Whether *tmdb_id* can be a TMDB id a title's ref is made of."""
    return tmdb_id is not None and 0 < tmdb_id <= _MAX_TMDB_ID


@router.get("/api/downloads/strip")
def api_downloads_strip(
    request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    try:
        answer = current(conn, secret_key)
    except strip.Unavailable:
        return respond_err("busy", status=503)
    return respond_ok(payload_for(conn, secret_key, username, answer))


def current(conn: sqlite3.Connection, secret_key: str) -> Answer:
    """The shared answer, asked of the services only when the last is past its time. Raises
    strip.Unavailable when there is none and none can be made."""
    return strip.shared.get(lambda before: _make(conn, secret_key, before))


def payload_for(
    conn: sqlite3.Connection, secret_key: str, username: str, answer: Answer
) -> dict[str, Any]:
    """What a page is told, with *username*'s own marks of what is ready, for its card in
    Recently added, and what critics scored each title as the ratings cache holds it: as the
    row's cards have them, and as /api/downloads gives them, as title_ref and scores."""
    shown = strip.payload(answer, admin=get_user_role(conn, username) == "admin")
    scored(conn, shown["items"])
    for item in shown["items"]:
        rated = "rt" in item or "imdb" in item
        item.update(
            title_ref=item["ref"],
            scores={"rt": item.get("rt"), "imdb": item.get("imdb")} if rated else None,
        )
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


def with_retrying(conn: sqlite3.Connection, data: DownloadsResponse) -> DownloadsResponse:
    """*data* with each download that needs attention saying what mediaman does about it
    unasked, and whether it does anything: not while another copy of its title still downloads;
    or that it stopped, as the same fault kept coming back."""
    items = [data["hero"], *data["queue"]] if data["hero"] else data["queue"]
    downloading = {i.get("retry_key") for i in items if i.get("state") == "downloading"}

    def marked(item: dict[str, object]) -> dict[str, object]:
        key = item.get("retry_key")
        if item.get("state") != "needs_attention" or not isinstance(key, str) or not key:
            return item
        reasons = item.get("attention_reasons")
        if not isinstance(reasons, list):
            reasons = [item.get("attention") or ""]
        fixing = "" if key in downloading else fixing_words(conn, key, [str(r) for r in reasons])
        stopped = "" if fixing else stopped_words(conn, key)
        label = fixing or stopped or item.get("state_label")
        return {
            **item,
            "retrying": bool(fixing),
            "fixing": fixing,
            "stopped": stopped,
            "state_label": label,
        }

    return cast(
        DownloadsResponse,
        {
            **data,
            "hero": marked(data["hero"]) if data["hero"] else None,
            "queue": [marked(item) for item in data["queue"]],
        },
    )


def _make(conn: sqlite3.Connection, secret_key: str, before: Strip) -> Strip:
    data = with_retrying(conn, build_downloads_response(conn, secret_key))
    items = strip.collect(data, before.items, now_utc())
    titled = tuple(_with_titles(conn, secret_key, items))
    return Strip(titled, data["outage"], len(data["upcoming"]))


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
    return mark_playable(conn, titled, index, arr)


def library_of(conn: sqlite3.Connection, secret_key: str) -> catalog.LibraryIndex:
    """The index of the scanned libraries' titles, as held."""
    return catalog.library_index(
        conn,
        TmdbClient.from_db(conn, secret_key),
        lib.scanned_library_ids(conn),
        lib.library_movies,
        lib.library_shows,
    )


def mark_playable(
    conn: sqlite3.Connection,
    items: list[StripItem],
    index: catalog.LibraryIndex,
    arr: catalog.ArrView | None = None,
) -> list[StripItem]:
    """*items*, of which one that is done but that the library does not hold yet says it was
    added, and is not offered to play."""
    now = now_utc()

    def waiting(item: StripItem) -> bool:
        if item.state != "ready":
            return False
        done = now - timedelta(seconds=strip.READY_S - (item.ready_seconds or 0))
        return not in_library(conn, index, item.kind, item.tmdb_id, item.title, done, arr)

    return [
        replace(item, line=strip.WAITING_LINE, playable=False) if waiting(item) else item
        for item in items
    ]


def in_library(
    conn: sqlite3.Connection,
    index: catalog.LibraryIndex,
    kind: str,
    tmdb_id: int | None,
    title: str,
    done: datetime,
    arr: catalog.ArrView | None = None,
) -> bool:
    """Whether the library holds a title ("movie" or a show's) whose download was done at
    *done*: of a show, only once the library was read after that, as the episodes it brought
    are not there before."""
    ref = library_title(index, kind, tmdb_id, title, arr=arr)
    if ref is None:
        return False
    if kind == "movie":
        return True
    row = conn.execute(
        "SELECT MAX(last_scanned_at) FROM media_items WHERE show_rating_key = ?",
        (ref.partition("-")[2],),
    ).fetchone()
    read = parse_iso_utc(row[0]) if row else None
    return read is not None and read >= done


def _with_title(
    item: StripItem, index: catalog.LibraryIndex, arr: catalog.ArrView, *, shown: bool
) -> StripItem:
    poster = catalog.tmdb_image_url(item.poster_url, "w342") or ""
    kind = "movie" if item.kind == "movie" else "tv"
    if item.tmdb_id is None or not is_tmdb_id(item.tmdb_id):
        poster = poster or _library_poster(index, kind, None, item.title)
        return replace(item, poster_url=poster)
    held = held_ref(index, arr, kind, item.tmdb_id, item.title)
    ref = held or f"tmdb-{kind}-{item.tmdb_id}"
    art = _fanart(item, arr)
    year = _arr_year(kind, item.tmdb_id, arr)
    if shown and not (poster and art):
        known = _held(kind, item.tmdb_id)
        poster = poster or known.get("poster_url") or ""
        art = art or known.get("backdrop_url") or ""
        told = known.get("year")
        year = year or (told if isinstance(told, int) and told > 0 else None)
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
        year=year,
        size_bytes=_file_size(item, arr) if item.state == "ready" else None,
    )


def held_ref(
    index: catalog.LibraryIndex, arr: catalog.ArrView, kind: str, tmdb_id: int, title: str
) -> str | None:
    """The library title ("movie" or a show's) a download of TMDB's *tmdb_id* ("movie" or
    "tv") opens; None when the library does not hold it."""
    held = index.library_ref(kind, tmdb_id, title, arr, year=_radarr_year(kind, tmdb_id, arr))
    if kind == "movie" and index.tmdb_of.get(held or "", tmdb_id) != tmdb_id:
        held = None  # a film is held under its own TMDB id; library_ref checks a show's
    return held or library_title(index, kind, tmdb_id, title, arr=arr)


def title_ref(
    index: catalog.LibraryIndex, arr: catalog.ArrView, kind: str, tmdb_id: int | None, title: str
) -> str | None:
    """The ref a download's title sheet opens at, as the row's cards have it: the library's
    title where it is held, else TMDB's; None without a TMDB id."""
    if tmdb_id is None or not is_tmdb_id(tmdb_id):
        return None
    return held_ref(index, arr, kind, tmdb_id, title) or f"tmdb-{kind}-{tmdb_id}"


def library_title(
    index: catalog.LibraryIndex,
    kind: str,
    tmdb_id: int | None,
    title: str,
    year: int | None = None,
    arr: catalog.ArrView | None = None,
) -> str | None:
    """The library title ("movie" or a show's) by its TMDB id where that is known; else the
    one of its title, with no TMDB id of its own where the title's is known, and of its year
    where both are known: *year*, else the one TMDB told before, else Radarr's of a film
    (when *arr* is given). None when it is not held."""
    movie = kind == "movie"
    found = (index.movies_by_tmdb if movie else index.shows_by_tmdb).get(tmdb_id or 0)
    if found is not None:
        return found
    by_title = index.movies_by_title if movie else index.shows_by_title
    held = by_title.get(catalog.normalise_title(title))
    if held is None or (tmdb_id and held in index.tmdb_of):
        return None
    held_year = index.year_of.get(held)
    if held_year and not year and tmdb_id:
        year = _year_told(kind, tmdb_id, arr)
    # One apart is one title, dated by two of its releases.
    return None if year and held_year and abs(year - held_year) > 1 else held


def _year_told(kind: str, tmdb_id: int, arr: catalog.ArrView | None) -> int | None:
    """The year TMDB told of a title before; of a film it told none of, Radarr's."""
    told = _held("movie" if kind == "movie" else "tv", tmdb_id).get("year")
    if isinstance(told, int):
        return told
    return _radarr_year(kind, tmdb_id, arr)


def _radarr_year(kind: str, tmdb_id: int, arr: catalog.ArrView | None) -> int | None:
    if kind != "movie" or arr is None:
        return None
    year = arr.caches["radarr_movies"].get(tmdb_id, {}).get("year")
    return year if isinstance(year, int) else None


def known_year(kind: str, tmdb_id: int, arr: catalog.ArrView) -> int | None:
    """The year Radarr gives of a film ("movie"), Sonarr of a show ("tv"), else the one TMDB
    told before; None when neither is known. Nobody is asked."""
    if year := _arr_year(kind, tmdb_id, arr):
        return year
    told = _held(kind, tmdb_id).get("year")
    return told if isinstance(told, int) and told > 0 else None


def _arr_year(kind: str, tmdb_id: int, arr: catalog.ArrView) -> int | None:
    """The year Radarr gives of a film, Sonarr of a show."""
    found = arr.caches["radarr_movies" if kind == "movie" else "sonarr_series"].get(tmdb_id)
    year = found.get("year") if found else None
    return year if isinstance(year, int) and year > 0 else None


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


def _file_size(item: StripItem, arr: catalog.ArrView) -> int | None:
    """How many bytes the file Radarr imported of a film holds; None of a show, as Sonarr tells
    only the whole show's."""
    if item.kind != "movie" or item.tmdb_id is None:
        return None
    file = arr.caches["radarr_movies"].get(item.tmdb_id, {}).get("movieFile") or {}
    size = file.get("size")
    return size if isinstance(size, int) and size > 0 else None
