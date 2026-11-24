"""Watch JSON for what lies beyond the library rows: TMDB search of titles not in the
library, the discover rows, a title's detail and a season's episodes, for admins and
viewers alike, and a library title's size, dates and keeps for admins only.

A title is named by a ref: ``movie-<key>`` and ``show-<key>`` for the library,
``tmdb-movie-<id>`` and ``tmdb-tv-<id>`` for TMDB, ``rec-<id>`` for a recommendation.
A malformed ref, or a library ref outside the scanned libraries, is a 404. TMDB not set
up is never an error for the library; Radarr or Sonarr out of reach only leaves out the
download state.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.format import format_bytes
from mediaman.core.time import now_iso, now_utc
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.downloads.notifications import has_unsent_notification
from mediaman.services.infra import disk_cache
from mediaman.services.media_meta.omdb import get_omdb_key, is_key_refused, lookup_ratings
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexError,
    PlexItemNotFound,
    is_in_libraries,
)
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai.recommendations.repository import fetch_suggestion_by_id
from mediaman.services.scheduled_actions import parse_execute_at
from mediaman.services.scheduled_actions.days_left import days_left
from mediaman.web.auth.middleware import get_current_admin, get_current_user
from mediaman.web.auth.password_hash import get_user_email, get_user_role
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.library_query import protection_label
from mediaman.web.repository.search import fetch_ratings_cache, upsert_ratings_cache
from mediaman.web.repository.watch_progress import get_progress, show_progress
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.download.confirm import validate_youtube_id
from mediaman.web.routes.search import _enrichment
from mediaman.web.routes.search._enrichment import _QUERY_LIMITER
from mediaman.web.routes.watch.pages import (
    _episode_row,
    _primary_episode,
    leaving_info,
    open_plex,
    play_href,
    plex_error_code,
    poster_url,
    progress_fields,
    resumable,
)

logger = logging.getLogger(__name__)

router = APIRouter()

REF_RE = re.compile(r"^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$|^rec-[0-9]{1,10}$")
_RATINGS_TTL = timedelta(days=30)
# OMDb with nothing to give (no rating yet, or the day's quota spent) is asked again the
# next day, not on every open.
_NO_RATINGS_TTL = timedelta(days=1)


class _OutsideLibraries(Exception):
    """Plex places a title the sync listed outside the scanned libraries."""


def _not_found() -> JSONResponse:
    return respond_err("not_found", status=404)


def _tmdb_client(request: Request, conn: sqlite3.Connection) -> TmdbClient | None:
    return TmdbClient.from_db(conn, request.app.state.config.secret_key)


def _arr(request: Request, conn: sqlite3.Connection) -> catalog.ArrView:
    return catalog.arr_view(conn, request.app.state.config.secret_key)


@router.get("/api/watch/search/tmdb")
def api_watch_search_tmdb(
    request: Request, q: str = "", username: str = Depends(get_current_user)
) -> JSONResponse:
    """TMDB titles matching *q* that the library does not hold."""
    term = q.strip()
    conn = get_db()
    client = _tmdb_client(request, conn)
    if client is None:
        return respond_ok({"tmdb_configured": False, "items": []})
    if len(term) < catalog.MIN_QUERY_LEN:
        return respond_ok({"tmdb_configured": True, "items": []})
    try:
        # Only a search TMDB is asked for counts against the limit; a cached one is free.
        found = catalog.search(client, term, allow_fetch=lambda: _QUERY_LIMITER.check(username))
    except disk_cache.Refused:
        return respond_err("too_many_requests", status=429)
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    index = catalog.library_index(
        conn, client, lib.scanned_library_ids(conn), lib.library_movies, lib.library_shows
    )
    arr = _arr(request, conn)
    items = [
        card
        for item in found
        if (card := catalog.tmdb_card(item, index, arr)) is not None and not card["in_library"]
    ]
    return respond_ok({"tmdb_configured": True, "items": items})


@router.get("/api/watch/discover")
def api_watch_discover(request: Request, username: str = Depends(get_current_user)) -> JSONResponse:
    """Trending this week, Popular movies and Popular TV, each card marked when the
    library holds it."""
    conn = get_db()
    client = _tmdb_client(request, conn)
    if client is None:
        return respond_ok({"tmdb_configured": False, "stale": False, "rows": []})
    try:
        shelves = catalog.discover_shelves(client)
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    index = catalog.library_index(
        conn, client, lib.scanned_library_ids(conn), lib.library_movies, lib.library_shows
    )
    arr = _arr(request, conn)
    rows = [
        {
            "id": shelf_id,
            "title": title,
            "items": [c for i in items if (c := catalog.tmdb_card(i, index, arr)) is not None],
        }
        for shelf_id, title, items in shelves
    ]
    return respond_ok({"tmdb_configured": True, "stale": False, "rows": rows})


def _ratings(
    conn: sqlite3.Connection,
    secret_key: str,
    kind: str,
    tmdb_id: int,
    title: str,
    year: int | None,
    allow_fetch: Callable[[], bool],
) -> dict[str, str]:
    """OMDb's IMDb, Rotten Tomatoes and Metascore ratings, from the ratings cache when fresh;
    none when OMDb has no key, is paused, or would be asked and *allow_fetch* says no. The
    pause after OMDb gives no answer is search's own, so either one spares the other. An answer with
    nothing in it is cached too, for a shorter time, but only when OMDb answered: a timeout
    or a spent quota caches nothing and leaves the ratings already held (old ones are shown
    in its place)."""
    media_type = "movie" if kind == "movie" else "tv"
    now = now_utc()
    stale: dict[str, str] = {}
    for row in fetch_ratings_cache(conn, [(tmdb_id, media_type)]):
        cached = {"imdb": row.imdb_rating, "rt": row.rt_rating, "metascore": row.metascore}
        found = {k: v for k, v in cached.items() if v}
        if row.fetched_at >= (now - (_RATINGS_TTL if found else _NO_RATINGS_TTL)).isoformat():
            return found
        stale = found
    omdb_key = get_omdb_key(conn, secret_key)
    if not omdb_key or is_key_refused(omdb_key) or time.monotonic() < _enrichment._paused_until:
        return stale
    if not allow_fetch():
        return stale
    data = lookup_ratings(title, year, media_type, omdb_key=omdb_key)
    if data is None:
        _enrichment._paused_until = time.monotonic() + _enrichment._NO_ANSWER_PAUSE_SECONDS
        return stale
    try:
        upsert_ratings_cache(
            conn,
            [
                (
                    tmdb_id,
                    media_type,
                    data.get("imdb"),
                    data.get("rt"),
                    data.get("metascore"),
                    now_iso(),
                )
            ],
        )
    except sqlite3.Error:
        logger.warning("watch: ratings_cache write failed", exc_info=True)
    return {**stale, **data}


def _payload(ref: str, kind: str, tmdb_configured: bool) -> dict[str, Any]:
    return {
        "ref": ref,
        "kind": kind,
        "in_library": False,
        "library_ref": None,
        "title": "",
        "year": None,
        "year_end": None,
        "runtime_min": None,
        "season_count": None,
        "genres": [],
        "certificate": None,
        "tagline": None,
        "overview": "",
        "backdrop_url": None,
        "poster_url": None,
        "director": None,
        "ratings": {"imdb": None, "rt": None, "tmdb": None, "metascore": None},
        "cast": [],
        "trailer_key": None,
        "reason": None,
        "play": None,
        "leaving": None,
        "keep_allowed": False,
        "seasons": [],
        "episodes": [],
        "download": None,
        "tmdb_configured": tmdb_configured,
    }


def _apply_tmdb(
    out: dict[str, Any],
    request: Request,
    conn: sqlite3.Connection,
    client: TmdbClient,
    kind: str,
    tmdb_id: int,
    username: str,
    allow_fetch: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Fill *out* from TMDB and OMDb, keeping any title, overview and poster already set
    from Plex; returns the TMDB fields. Raises TmdbUnavailable, and disk_cache.Refused
    when TMDB would be asked and *allow_fetch* says no. OMDb is asked only within the
    query limit: *allow_fetch*, or *username*'s own when it is not given."""
    fields = catalog.details(client, kind, tmdb_id, allow_fetch=allow_fetch)
    for name in (
        "year",
        "year_end",
        "runtime_min",
        "season_count",
        "genres",
        "certificate",
        "tagline",
        "backdrop_url",
        "director",
        "cast",
    ):
        if fields[name] or out[name] in (None, []):
            out[name] = fields[name]
    for name in ("title", "overview", "poster_url"):
        out[name] = out[name] or fields[name]
    out["trailer_key"] = validate_youtube_id(fields["trailer_key"])
    ratings = _ratings(
        conn,
        request.app.state.config.secret_key,
        kind,
        tmdb_id,
        out["title"],
        out["year"],
        allow_fetch or (lambda: _QUERY_LIMITER.check(username)),
    )
    out["ratings"] = {
        "imdb": ratings.get("imdb"),
        "rt": ratings.get("rt"),
        "tmdb": fields["tmdb_rating"],
        "metascore": ratings.get("metascore"),
    }
    return fields


def _movie_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    is_admin: bool,
    libraries: list[str],
    movie: lib.LibraryMovie,
) -> dict[str, Any]:
    ref = f"movie-{movie.key}"
    client = _tmdb_client(request, conn)
    out = _payload(ref, "movie", client is not None)
    out.update(
        in_library=True, library_ref=ref, title=movie.title, poster_url=poster_url(movie.key)
    )
    try:
        item = open_plex(request, conn).get_item(movie.key)
    except PlexError:
        item = None
    if item is not None and (item.type != "movie" or not is_in_libraries(item, libraries)):
        raise _OutsideLibraries(movie.key)
    if item is not None:
        out.update(
            title=item.title or movie.title,
            year=item.year,
            overview=item.summary,
            runtime_min=round(item.duration_s / 60) or None,
        )
        progress = get_progress(conn, username, [movie.key]).get(movie.key)
        origin = f"/watch#title={ref}"
        if progress is not None and resumable(progress):
            pct, left = progress_fields(progress)
            out["play"] = {
                "label": "Resume",
                "href": play_href(movie.key, resume=True, origin=origin),
                "progress_pct": pct,
                "minutes_left": left,
            }
        else:
            out["play"] = {
                "label": "Play",
                "href": play_href(movie.key, origin=origin),
                "progress_pct": None,
                "minutes_left": None,
            }
    now = now_utc()
    out["leaving"] = leaving_info(lib.pending_deletion(conn, libraries, movie.key, now), now)
    out["keep_allowed"] = is_admin or out["leaving"] is not None
    if client is not None and movie.tmdb_id:
        with contextlib.suppress(catalog.TmdbUnavailable):
            _apply_tmdb(out, request, conn, client, "movie", movie.tmdb_id, username)
    return out


def _show_tmdb_id(
    request: Request, conn: sqlite3.Connection, client: TmdbClient, listed: lib.LibraryShow
) -> int | None:
    own, _year = catalog.library_ids(conn).get(f"show-{listed.key}", (None, None))
    if own or not listed.tvdb_id:
        return own
    return _arr(request, conn).tmdb_id_for_tvdb(listed.tvdb_id) or catalog.tmdb_id_for_tvdb(
        client, listed.tvdb_id
    )


def _show_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    is_admin: bool,
    libraries: list[str],
    listed: lib.LibraryShow,
) -> dict[str, Any]:
    ref = f"show-{listed.key}"
    client = _tmdb_client(request, conn)
    out = _payload(ref, "show", client is not None)
    out.update(
        in_library=True,
        library_ref=ref,
        title=listed.title,
        poster_url=poster_url(listed.key),
        season_count=listed.season_count or None,
    )
    origin = f"/watch#title={ref}"
    try:
        plex = open_plex(request, conn)
        show = plex.get_show(listed.key)
        if not is_in_libraries(show, libraries):
            raise _OutsideLibraries(listed.key)
        episodes_cache: dict[str, list[PlexEpisode]] = {}

        def episodes_of(season_key: str) -> list[PlexEpisode]:
            if season_key not in episodes_cache:
                episodes_cache[season_key] = plex.get_season_episodes(season_key)
            return episodes_cache[season_key]

        progress = show_progress(conn, username, listed.key)
        primary, primary_season = _primary_episode(plex, show, progress, episodes_of, origin)
        selected = next((s for s in show.seasons if s.index == primary_season), None)
        if selected is None and show.seasons:
            selected = show.seasons[0]
        episodes = episodes_of(selected.rating_key) if selected else []
    except PlexError:
        show = None
    if show is not None:
        out.update(
            title=show.title or listed.title,
            year=show.year_first,
            year_end=show.year_last if show.year_last != show.year_first else None,
            overview=show.summary,
            genres=[show.genre] if show.genre else [],
            season_count=len(show.seasons) or None,
            play={**primary, "progress_pct": None, "minutes_left": None},
            seasons=[
                {
                    "number": s.index,
                    "name": s.title or f"Season {s.index}",
                    "episode_count": len(episodes_cache[s.rating_key])
                    if s.rating_key in episodes_cache
                    else None,
                    "in_library": True,
                    "selected": s is selected,
                }
                for s in show.seasons
            ],
            episodes=[_episode_row(e, progress.get(e.rating_key), origin) for e in episodes],
        )
    now = now_utc()
    leaving = next(
        (
            p
            for p in lib.pending_deletions(conn, libraries, now)
            if p.show_key == listed.key and p.season_number is not None
        ),
        None,
    )
    out["leaving"] = leaving_info(leaving, now)
    out["keep_allowed"] = is_admin or leaving is not None
    tmdb_id = _show_tmdb_id(request, conn, client, listed) if client is not None else None
    if client is not None and tmdb_id is not None:
        try:
            fields = _apply_tmdb(out, request, conn, client, "tv", tmdb_id, username)
        except catalog.TmdbUnavailable:
            pass
        else:
            counts = {s["number"]: s["episode_count"] for s in fields["tmdb_seasons"]}
            for season in out["seasons"]:
                if season["episode_count"] is None:
                    season["episode_count"] = counts.get(season["number"])
    return out


def _tmdb_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    client: TmdbClient,
    ref: str,
    kind: str,
    tmdb_id: int,
    download_url: str | None,
    allow_fetch: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """A TMDB title (*kind* "movie" or "tv"); raises TmdbUnavailable and, as
    ``_apply_tmdb``, disk_cache.Refused."""
    out = _payload(ref, "movie" if kind == "movie" else "show", True)
    fields = _apply_tmdb(out, request, conn, client, kind, tmdb_id, username, allow_fetch)
    arr = _arr(request, conn)
    index = catalog.library_index(
        conn, client, lib.scanned_library_ids(conn), lib.library_movies, lib.library_shows
    )
    library_ref = index.library_ref(
        kind, tmdb_id, fields["title"], arr, year=fields["year"], tvdb_id=fields["tvdb_id"]
    )
    if library_ref is not None:
        out.update(in_library=True, library_ref=library_ref)
    if kind == "tv":
        held = arr.seasons_with_files(tmdb_id)
        out["seasons"] = [
            {**s, "in_library": s["number"] in held, "selected": False}
            for s in fields["tmdb_seasons"]
        ]
        if library_ref is None:
            out["sonarr_tracked"] = arr.sonarr_tracked(tmdb_id)
    if library_ref is None:
        state, pct = arr.state(kind, tmdb_id)
        request_body: dict[str, Any] = (
            {"url": download_url}
            if download_url
            else {
                "url": "/api/search/download",
                "body": {"media_type": kind, "tmdb_id": tmdb_id, "title": out["title"]},
            }
        )
        service = "radarr" if kind == "movie" else "sonarr"
        email = get_user_email(conn, username)
        out["download"] = {
            "state": state,
            "pct": pct,
            "answered": arr.answers_for(kind),
            "notify": email is not None
            and has_unsent_notification(
                conn,
                email=email,
                service=service,
                tmdb_id=tmdb_id,
                tvdb_id=(fields["tvdb_id"] or arr.tvdb_id(tmdb_id)) if kind == "tv" else None,
            ),
            "service": service,
            "tmdb_id": tmdb_id,
            "request": request_body,
        }
    return out


@router.get("/api/watch/title/{ref}")
def api_watch_title(
    ref: str, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    if not REF_RE.fullmatch(ref):
        return _not_found()
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    is_admin = get_user_role(conn, username) == "admin"
    kind, _, rest = ref.partition("-")
    try:
        if kind == "movie":
            movie = lib.find_movie(conn, libraries, rest)
            if movie is None:
                return _not_found()
            return respond_ok(_movie_title(request, conn, username, is_admin, libraries, movie))
        if kind == "show":
            listed = lib.find_show(conn, libraries, rest)
            if listed is None:
                return _not_found()
            return respond_ok(_show_title(request, conn, username, is_admin, libraries, listed))
    except _OutsideLibraries:
        return _not_found()

    client = _tmdb_client(request, conn)
    if client is None:
        return respond_err("tmdb_not_configured", status=503)
    try:
        if kind == "tmdb":
            media_type, _, tmdb_id = rest.partition("-")
            # Any id can be asked for, so only a title TMDB or OMDb must answer counts
            # against the limit, once per request; a cached one is free.
            allowed: list[bool] = []

            def allow_fetch() -> bool:
                if not allowed:
                    allowed.append(_QUERY_LIMITER.check(username))
                return allowed[0]

            return respond_ok(
                _tmdb_title(
                    request,
                    conn,
                    username,
                    client,
                    ref,
                    media_type,
                    int(tmdb_id),
                    None,
                    allow_fetch,
                )
            )
        rec = fetch_suggestion_by_id(conn, int(rest))
        if rec is None or rec.media_type not in ("movie", "tv"):
            return _not_found()
        if rec.tmdb_id:
            out = _tmdb_title(
                request,
                conn,
                username,
                client,
                ref,
                rec.media_type,
                rec.tmdb_id,
                f"/api/recommended/{rec.id}/download",
            )
        else:
            out = _payload(ref, "movie" if rec.media_type == "movie" else "show", True)
            out.update(
                title=rec.title,
                year=rec.year,
                overview=rec.description or "",
                poster_url=catalog.tmdb_image_url(rec.poster_url, "w500"),
            )
            out["ratings"].update(tmdb=rec.rating, rt=rec.rt_rating)
    except disk_cache.Refused:
        return respond_err("too_many_requests", status=429)
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    out["reason"] = rec.reason
    return respond_ok(out)


@router.get("/api/watch/title/{ref}/season/{number}")
def api_watch_title_season(
    ref: str, number: int, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """The episodes of one season of a library show, with this person's progress."""
    if not REF_RE.fullmatch(ref) or not ref.startswith("show-") or not 0 <= number <= 9999:
        return _not_found()
    key = ref.removeprefix("show-")
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    if lib.find_show(conn, libraries, key) is None:
        return _not_found()
    try:
        plex = open_plex(request, conn)
        show = plex.get_show(key)
        if not is_in_libraries(show, libraries):
            return _not_found()
        season = next((s for s in show.seasons if s.index == number), None)
        if season is None:
            return _not_found()
        episodes = plex.get_season_episodes(season.rating_key)
    except PlexItemNotFound:
        return _not_found()
    except PlexError as exc:
        return respond_err(plex_error_code(exc), status=503)
    progress = show_progress(conn, username, key)
    origin = f"/watch#title={ref}"
    return respond_ok(
        {"episodes": [_episode_row(e, progress.get(e.rating_key), origin) for e in episodes]}
    )


def _keep(facts: lib.TitleFacts) -> dict[str, str]:
    if facts.show_keep is not None:
        return {"state": "show_kept", "label": protection_label(*facts.show_keep) or "Kept"}
    keeps = list(facts.item_keeps.values())
    total = len(facts.item_ids)
    if keeps and len(keeps) == total:
        if all(action == "protected_forever" for action, _ in keeps):
            return {"state": "forever", "label": "Kept forever"}
        soonest = min((k for k in keeps if k[0] != "protected_forever"), key=lambda k: k[1] or "")
        return {"state": "kept", "label": protection_label(*soonest) or "Kept"}
    if keeps:
        return {"state": "none", "label": f"{len(keeps)} of {total} seasons kept"}
    return {"state": "none", "label": "Not kept"}


def _deletion(
    conn: sqlite3.Connection, libraries: list[str], facts: lib.TitleFacts, now: datetime
) -> dict[str, Any] | None:
    ids = set(facts.item_ids)
    soonest = next(
        (p for p in lib.pending_deletions(conn, libraries, now) if p.media_item_id in ids), None
    )
    if soonest is None:
        return None
    execute_at = parse_execute_at(soonest.execute_at, default=now)
    return {"execute_at": execute_at.isoformat(), "days": days_left(execute_at, now)}


@router.get("/api/watch/admin/title/{ref}")
def api_watch_admin_title(ref: str, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Size, dates, deletion and keeps of a library title, and what its Keep and Delete
    controls act on."""
    if not REF_RE.fullmatch(ref) or not ref.startswith(("movie-", "show-")):
        return _not_found()
    kind, _, key = ref.partition("-")
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    now = now_utc()
    facts = lib.title_facts(conn, libraries, kind, key, now)
    if facts is None:
        return _not_found()
    keep = _keep(facts)
    is_show = kind == "show"
    delete: dict[str, Any] | None
    if keep["state"] != "none":
        delete = None
    elif is_show:
        delete = {"kind": "seasons", "show_rk": key, "title": facts.title}
    else:
        delete = {"kind": "movie", "id": key, "title": facts.title}
    return respond_ok(
        {
            "size": format_bytes(facts.size_bytes),
            "size_bytes": facts.size_bytes,
            "added": facts.added_at,
            "last_watched": facts.last_watched_at,
            "deletion": _deletion(conn, libraries, facts, now),
            "keep": keep,
            "actions": {
                "keep_wrapper": {
                    "id": facts.item_ids[0],
                    "is_tv": is_show,
                    "show_rk": key if is_show else "",
                    "has_rk": is_show,
                    "show_title": facts.title,
                },
                "delete": delete,
            },
        }
    )


__all__ = ["REF_RE", "router"]
