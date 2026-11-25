"""The Watch page's row of what the person asked for and has not watched, as JSON: what is
there, then what is on its way, then what is not out yet.

Only the person's own requests, an admin's too. TMDB is never asked for it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal, cast, get_args

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc, parse_iso_strict_utc, parse_iso_utc
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.arr._types import RadarrMovie, SonarrSeries
from mediaman.services.arr.state import (
    ACTION_DOWNLOADING,
    ACTION_IN_LIBRARY,
    ACTION_PARTIAL,
    ACTION_QUEUED,
    season_on_disk,
    series_still_to_air,
)
from mediaman.services.downloads.download_format import extract_poster_url, next_movie_release
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import asked
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.watch_progress import get_progress
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.catalog_api import held_fields
from mediaman.web.routes.watch.pages import movie_subtitle, poster_url, resumable, show_subtitle

router = APIRouter()

# The page asks again when it is come back to and, while something downloads, once a minute.
_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60)

_LIMIT = 200
# A file Plex does not list by then is not one the library will hold.
_LISTED_WITHIN = timedelta(days=2)

State = Literal["ready", "arriving", "downloading", "queued", "coming"]
# Where each state comes in the row.
_ORDER = get_args(State)


@router.get("/api/watch/asked")
def api_watch_asked(request: Request, username: str = Depends(get_current_user)) -> JSONResponse:
    """``items`` are cards as of a discover row, each with its ``state`` (ready, arriving: there
    but not yet in Plex, downloading, queued or coming), ``started`` and ``arrived_at`` of a
    ready one and ``release_date`` of one that is coming, None when unknown. ``held_ref`` is the
    library's own title of it, for a season on its way too, or None. ``complete`` is False when
    Radarr or Sonarr, set up, did not answer, so that some on their way may be missing."""
    if not _LIMITER.check(username):
        return respond_err("too_many_requests", status=429)
    conn = get_db()
    requests = asked.requests_of(conn, username)
    if not requests:
        return respond_ok({"items": [], "complete": True})
    secret_key = request.app.state.config.secret_key
    libraries = lib.scanned_library_ids(conn)
    index = catalog.library_index(
        conn,
        TmdbClient.from_db(conn, secret_key),
        libraries,
        lib.library_movies,
        lib.library_shows,
    )
    row = _Row(
        conn,
        username,
        asked.held_library(conn, username, libraries, index),
        catalog.arr_view(conn, secret_key),
        held_fields(conn, username, libraries),
    )
    items = row.items(requests)
    complete = all(row.arr.answers_for(kind) for kind in row.asked_of)
    return respond_ok({"items": items[:_LIMIT], "complete": complete})


@dataclass(slots=True)
class _Row:
    conn: sqlite3.Connection
    username: str
    held: asked.Held
    arr: catalog.ArrView
    fields: Callable[[str | None], dict[str, object]]
    now: datetime = field(default_factory=now_utc)
    #: The kinds of title Radarr or Sonarr was asked about.
    asked_of: set[str] = field(default_factory=set)

    def items(self, requests: Sequence[asked.Request]) -> list[dict[str, Any]]:
        """A card per title, in the best state of any of its requests."""
        known = [self._with_ids(r) for r in requests]
        titles = [self._title_of(r) for r in known]
        refs = [
            title if title and self.held.is_there(title, request.season) else None
            for request, title in zip(known, titles, strict=True)
        ]
        # By its name alone, what the library holds may be another title than the one deleted.
        unsure = [
            request
            for request, ref in zip(known, refs, strict=True)
            if ref is None or not _has_id(request)
        ]
        over = asked.is_over(self.conn, self.username, unsure)
        items = [
            item
            for request, title, ref in zip(known, titles, refs, strict=True)
            if (item := self._item(request, title, ref, request in over)) is not None
        ]
        # A title may be known by its own ref in one request and by TMDB's in another.
        shown: set[str] = set()
        unique = []
        for item in sorted(items, key=_place):
            if not {item["key"], item["ref"]} & shown:
                unique.append(item)
            shown |= {item["key"], item["ref"]}
        return unique

    def _with_ids(self, request: asked.Request) -> asked.Request:
        """*request* with the ids Radarr and Sonarr have for it. By its name only where one of
        all their titles has it."""
        series = self.arr.caches["sonarr_series"]
        if request.tmdb_id:
            return request
        if request.tvdb_id:
            return replace(request, tmdb_id=self.arr.tmdb_id_for_tvdb(request.tvdb_id))
        name = catalog.normalise_title(request.title)
        movies = self.arr.caches["radarr_movies"]
        found: list[tuple[asked.Kind, int]] = [
            *(("movie", i) for i, m in movies.items() if _named(m, name)),
            *(("tv", i) for i, s in series.items() if _named(s, name)),
        ]
        found = [f for f in found if request.kind in (None, f[0])]
        if len(found) != 1:
            return request
        kind, tmdb_id = found[0]
        tvdb_id = self.arr.tvdb_id(tmdb_id) if kind == "tv" else None
        return replace(request, kind=kind, tmdb_id=tmdb_id, tvdb_id=tvdb_id)

    def _title_of(self, request: asked.Request) -> str | None:
        """The library title of what was asked for, with its season or without, or None."""
        ref = self.held.ref_of(request)
        if ref is None and request.kind and request.tmdb_id:
            found = self._entry(request.kind, request.tmdb_id)
            year = found.get("year")
            ref = self.held.index.library_ref(
                request.kind,
                request.tmdb_id,
                request.title or str(found.get("title") or ""),
                self.arr,
                year=year if isinstance(year, int) else None,
                tvdb_id=request.tvdb_id,
            )
        return ref if ref and self.held.is_there(ref, None) else None

    def _item(
        self, request: asked.Request, title: str | None, ref: str | None, over: bool
    ) -> dict[str, Any] | None:
        if ref is not None:
            watched = self.held.is_watched(ref, request.season)
            return None if over or watched else self._ready(ref, request)
        if request.kind is None or not request.tmdb_id:
            return None
        self.asked_of.add(request.kind)
        state, release_date = self._on_its_way(request, request.tmdb_id)
        # Of a show, what is not out yet is none of what was deleted or played.
        if state is None or (over and (request.kind == "movie" or state != "coming")):
            return None
        item = self._on_its_way_card(request, request.kind, request.tmdb_id, state, release_date)
        return {**item, "held_ref": title}

    def _entry(self, kind: str, tmdb_id: int) -> Mapping[str, Any]:
        caches = self.arr.caches
        if kind == "movie":
            return caches["radarr_movies"].get(tmdb_id, RadarrMovie())
        return caches["sonarr_series"].get(tmdb_id, SonarrSeries())

    def _on_its_way(
        self, request: asked.Request, tmdb_id: int
    ) -> tuple[State | None, datetime | None]:
        """The state of a title the library lacks, and when one that is not out is. None when
        nothing fetches it: cancelled, given up on or deleted."""
        kind = "movie" if request.kind == "movie" else "tv"
        entry = self._entry(kind, tmdb_id)
        state, _pct = self.arr.state(kind, tmdb_id)
        if kind == "tv" and request.season is not None:
            state = self._season_state(entry, request.season, state)
        if state in (ACTION_IN_LIBRARY, ACTION_PARTIAL):
            added = parse_iso_utc((entry.get("movieFile") or {}).get("dateAdded"))
            fresh = added is not None and self.now - added <= _LISTED_WITHIN
            return ("arriving" if fresh or request.waiting else None), None
        if state == ACTION_DOWNLOADING:
            return "downloading", None
        if state != ACTION_QUEUED:
            return None, None
        if kind == "movie":
            if entry.get("isAvailable"):
                return "queued", None
            return "coming", next_movie_release(entry)
        coming, airs = series_still_to_air(cast("SonarrSeries", entry), request.season)
        return ("coming", parse_iso_utc(airs)) if coming else ("queued", None)

    @staticmethod
    def _season_state(entry: Mapping[str, Any], number: int, state: str | None) -> str | None:
        season = next(
            (s for s in entry.get("seasons") or [] if s.get("seasonNumber") == number), None
        )
        if season is None:
            return None
        if season_on_disk(season):
            return ACTION_IN_LIBRARY
        if state == ACTION_DOWNLOADING:
            return state
        return ACTION_QUEUED if season.get("monitored") and entry.get("monitored", True) else None

    def _ready(self, ref: str, request: asked.Request) -> dict[str, Any]:
        kind, _, key = ref.partition("-")
        title: lib.LibraryMovie | lib.LibraryShow
        if kind == "movie":
            title = self.held.movies[key]
            subtitle = movie_subtitle(title)
            started = resumable(get_progress(self.conn, self.username, [key]).get(key))
            tmdb_id = title.tmdb_id or request.tmdb_id
        else:
            title = self.held.shows[key]
            seen = self.held.seen_shows.get(key, lib.ShowSeen())
            subtitle = show_subtitle(title, seen)
            # Not by a season seen to its end: the next one, just come, is new.
            started = any(s.seen and not s.watched for s in seen.seasons.values())
            tmdb_id = request.tmdb_id
        added_at = parse_iso_strict_utc(title.added_at)
        return {
            **catalog.discover_card(
                ref=ref,
                kind="movie" if kind == "movie" else "show",
                title=title.title,
                year=title.year,
                poster_url=poster_url(key),
                rating=None,
                library_ref=ref,
                download_state=None,
                download_pct=None,
            ),
            **self.fields(ref),
            "tmdb_id": tmdb_id,
            "key": _key(kind, tmdb_id, ref),
            "held_ref": ref,
            "subtitle": subtitle,
            "state": "ready",
            "started": started,
            "arrived_at": added_at.isoformat() if added_at else None,
            "release_date": None,
        }

    def _on_its_way_card(
        self,
        request: asked.Request,
        kind: asked.Kind,
        tmdb_id: int,
        state: State,
        release_date: datetime | None,
    ) -> dict[str, Any]:
        entry = self._entry(kind, tmdb_id)
        details = catalog.held_details(kind, tmdb_id) or {}
        poster = catalog.tmdb_image_url(extract_poster_url(entry.get("images")), "w342")
        year = entry.get("year") or details.get("year")
        download_state, download_pct = self.arr.state(kind, tmdb_id)
        ref = f"tmdb-{kind}-{tmdb_id}"
        return {
            **catalog.discover_card(
                ref=ref,
                kind="movie" if kind == "movie" else "show",
                title=str(entry.get("title") or request.title),
                year=year if isinstance(year, int) else None,
                poster_url=poster or details.get("poster_url"),
                rating=None,
                library_ref=None,
                download_state=download_state if state == "downloading" else None,
                download_pct=download_pct,
            ),
            **self.fields(None),
            "tmdb_id": tmdb_id,
            "key": _key(kind, tmdb_id, ref),
            "subtitle": None,
            "state": state,
            "started": False,
            "arrived_at": None,
            "release_date": release_date.date().isoformat() if release_date else None,
        }


def _has_id(request: asked.Request) -> bool:
    return bool(request.tmdb_id or request.tvdb_id)


def _named(entry: Mapping[str, Any], name: str) -> bool:
    return bool(name) and catalog.normalise_title(str(entry.get("title") or "")) == name


def _key(kind: str, tmdb_id: int | None, ref: str) -> str:
    """What a card stays under when its title comes into the library."""
    return f"{'movie' if kind == 'movie' else 'tv'}-{tmdb_id}" if tmdb_id else ref


def _place(item: dict[str, Any]) -> tuple[int, bool, float, str]:
    """Where a card stands: ready ones by when they came, the newest first and those begun last,
    coming ones by when they do, those with no date last."""
    when = 0.0
    if item["state"] == "ready":
        arrived = parse_iso_utc(item["arrived_at"])
        when = -arrived.timestamp() if arrived else 0.0
    elif item["state"] == "coming":
        out = parse_iso_utc(item["release_date"])
        when = out.timestamp() if out else float("inf")
    return _ORDER.index(item["state"]), item["started"], when, item["title"].casefold()


__all__ = ["router"]
