"""Watch JSON for what lies beyond the library rows: TMDB search of titles not in the
library, the discover rows, a title's detail and a season's episodes, for admins and
viewers alike, and a library title's size, dates and keeps for admins only.

A title is named by a ref: ``movie-<key>`` and ``show-<key>`` for the library,
``tmdb-movie-<id>`` and ``tmdb-tv-<id>`` for TMDB, ``rec-<id>`` for a shared
recommendation and ``pick-<id>`` for one of the person's own, which no one else has.
A malformed ref, or a library ref outside the scanned libraries, is a 404. TMDB not set
up is never an error for the library; Radarr or Sonarr out of reach only leaves out the
download state.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from mediaman.core.format import format_bytes
from mediaman.core.time import now_iso, now_utc
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.arr.state import ACTION_IN_LIBRARY, ACTION_PARTIAL
from mediaman.services.downloads.notifications import has_unsent_notification
from mediaman.services.infra import disk_cache
from mediaman.services.media_meta.item_enrichment import tidy_summary
from mediaman.services.media_meta.omdb import get_omdb_key, is_key_refused, lookup_ratings
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexError,
    PlexItemNotFound,
    PlexSeason,
    is_in_libraries,
)
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai.recommendations.repository import (
    SuggestionDetail,
    fetch_suggestion_by_id,
)
from mediaman.services.openai.recommendations.user_picks import fetch_pick
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import parse_execute_at
from mediaman.services.scheduled_actions.days_left import days_left
from mediaman.web.auth.middleware import get_current_admin, get_current_user
from mediaman.web.auth.password_hash import get_user_email, get_user_role
from mediaman.web.repository import seen_titles
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.kept import fetch_keep
from mediaman.web.repository.library_query import TitleSearch, protection_label
from mediaman.web.repository.search import fetch_ratings_cache, upsert_ratings_cache
from mediaman.web.repository.watch_progress import WatchProgress, get_progress, show_progress
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.download.confirm import validate_youtube_id
from mediaman.web.routes.search import _enrichment
from mediaman.web.routes.search._enrichment import _QUERY_LIMITER
from mediaman.web.routes.watch.browse_api import library_items
from mediaman.web.routes.watch.card_marks import (
    card_marks,
    latest_season_out,
    marked,
    title_key,
    tmdb_ref,
)
from mediaman.web.routes.watch.pages import (
    _episode_row,
    _primary_episode,
    _season_watched,
    leaving_info,
    open_plex,
    play_href,
    plex_error_code,
    poster_url,
    progress_fields,
    resumable,
    season_fields,
    seasons_leaving,
)
from mediaman.web.routes.watch.title_seasons import (
    begin_arr_view,
    fill_season_status,
    missing_seasons,
)

logger = logging.getLogger(__name__)

router = APIRouter()

REF_RE = re.compile(
    r"^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$|^(rec|pick)-[0-9]{1,10}$"
)
_SHELF_IDS = frozenset(shelf[0] for shelf in catalog.SHELVES)
# Pages of discover rows TMDB is asked for, apart from the query limit so a long scroll does
# not use up the day's searches. The day's cap is each of the three lists to its end.
_PAGE_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60, max_per_day=1500)
# Titles whose ratings OMDb is asked for, apart from the query limit (and as tight) so that
# opening a title, which counts there, is not counted twice.
_RATINGS_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
# Films whose collection TMDB is asked for, apart from the query limit (and as tight) for the
# same reason.
_COLLECTION_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
# Titles a preview asks for before they are opened: most never are, and each can cost Plex a call.
_AHEAD_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60, max_per_day=600)
# A worker thread waits for the answer.
_TMDB_TIMEOUT_S = 3.0
_RATINGS_TTL = timedelta(days=30)
# OMDb with nothing to give (no rating yet, or the day's quota spent) is asked again the
# next day, not on every open.
_NO_RATINGS_TTL = timedelta(days=1)

# Titles OMDb gave no answer for, and when each may be asked again: one title it cannot
# answer for must not leave the other titles' ratings empty.
_TITLE_PAUSE_SECONDS = 900.0
_TITLE_PAUSE_MAX = 512
_title_paused: dict[tuple[int, str], float] = {}
# Opens run in threads: the map, the streak and the pause below are touched under this lock.
_pause_lock = threading.Lock()
# OMDb down answers no title, and each first open would wait out its timeout: after this many
# different titles in a row with no answer, none is asked for a minute; any answer resets it.
_OUTAGE_STREAK = 3
_OUTAGE_PAUSE_SECONDS = 60.0
_unanswered_streak = 0
_outage_until = 0.0


class _OutsideLibraries(Exception):
    """Plex places a title the sync listed outside the scanned libraries."""


def _not_found() -> JSONResponse:
    return respond_err("not_found", status=404)


def _tmdb_client(request: Request, conn: sqlite3.Connection) -> TmdbClient | None:
    return TmdbClient.from_db(
        conn, request.app.state.config.secret_key, timeout=_TMDB_TIMEOUT_S, retry=False
    )


def _arr(request: Request, conn: sqlite3.Connection) -> catalog.ArrView:
    return catalog.arr_view(conn, request.app.state.config.secret_key)


@router.get("/api/watch/search/tmdb")
def api_watch_search_tmdb(
    request: Request, q: str = "", username: str = Depends(get_current_user)
) -> JSONResponse:
    """TMDB titles matching *q* that the library does not hold, and the people it names.
    ``held`` is the library titles among TMDB's that the library's own search does not find,
    as that search answers them."""
    term = q.strip()
    conn = get_db()
    client = _tmdb_client(request, conn)
    if client is None:
        return respond_ok({"tmdb_configured": False, "items": [], "held": [], "people": []})
    if not term:
        return respond_ok({"tmdb_configured": True, "items": [], "held": [], "people": []})
    try:
        # Only a search TMDB is asked for counts against the limit; a cached one is free.
        found = catalog.search(client, term, allow_fetch=lambda: _QUERY_LIMITER.check(username))
    except (disk_cache.Refused, catalog.TmdbBusy):
        return respond_err("too_many_requests", status=429)
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    libraries = lib.scanned_library_ids(conn)
    index = catalog.library_index(conn, client, libraries, lib.library_movies, lib.library_shows)
    arr = _arr(request, conn)
    cards = [card for item in found if (card := catalog.tmdb_card(item, index, arr)) is not None]
    marks = card_marks(seen_titles.person_marks(conn, username), client)
    refs = dict.fromkeys(cast(str, c["library_ref"]) for c in cards if c["in_library"])
    search = TitleSearch(conn, term)
    held = [
        title
        for ref in refs
        if (title := _library_title(conn, libraries, ref)) is not None
        and search.distance(title.title) is None
    ]
    return respond_ok(
        {
            "tmdb_configured": True,
            "items": [
                marked({**card, **lib.mark_fields(None, set())}, marks)
                for card in cards
                if not card["in_library"]
            ],
            "held": library_items(conn, username, libraries, held),
            "people": catalog.people(found),
        }
    )


def _library_title(
    conn: sqlite3.Connection, libraries: list[str], ref: str
) -> lib.LibraryMovie | lib.LibraryShow | None:
    kind, _, key = ref.partition("-")
    find = lib.find_movie if kind == "movie" else lib.find_show
    return find(conn, libraries, key)


@router.get("/api/watch/discover")
def api_watch_discover(
    request: Request,
    row: str | None = None,
    page: int = Query(2, ge=2, le=catalog.MAX_PAGE),
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """Trending this week, Popular movies and Popular TV, each card marked when the
    library holds it; with *row*, a later page of that one row instead. ``next_page`` is
    the page to go on with, or None at the end of the list."""
    if row is not None and row not in _SHELF_IDS:
        return _not_found()
    conn = get_db()
    client = _tmdb_client(request, conn)
    if client is None:
        return respond_ok({"tmdb_configured": False, "stale": False, "rows": []})
    if row is not None:
        return _discover_page(request, conn, client, row, page, username)
    try:
        shelves = catalog.discover_shelves(client)
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    cards = _discover_cards(request, conn, client, username, [items for _, _, items in shelves])
    marks = card_marks(seen_titles.person_marks(conn, username), client)
    rows = [
        {
            "id": shelf_id,
            "title": title,
            "items": [marked(card, marks) for card in row_cards],
            "next_page": 2,
        }
        for (shelf_id, title, _), row_cards in zip(shelves, cards, strict=True)
    ]
    return respond_ok({"tmdb_configured": True, "stale": False, "rows": rows})


def held_fields(
    conn: sqlite3.Connection, username: str, libraries: list[str]
) -> Callable[[str | None], dict[str, object]]:
    """What a TMDB title's card says of the library title it is, by that title's ref:
    *username*'s mark, of a film that is leaving, when and what a Keep of it keeps, and of a
    show, when its first leaving season does."""
    seen = lib.seen_refs(conn, username, libraries)
    now = now_utc()
    pending = lib.pending_deletions(conn, libraries)
    leaving = {
        f"movie-{p.media_item_id}": leaving_info(p, now) for p in pending if p.season_number is None
    }
    seasons = {f"show-{key}": found for key, found in seasons_leaving(pending, now).items()}

    def fields(ref: str | None) -> dict[str, object]:
        found = leaving.get(ref or "") or {"days": None, "keep_id": None}
        season = seasons.get(ref or "")
        return {
            **lib.mark_fields(ref, seen),
            "leaving_days": season[0]["days"] if season else found["days"],
            "keep_id": found["keep_id"],
            **season_fields(season),
        }

    return fields


def _discover_cards(
    request: Request,
    conn: sqlite3.Connection,
    client: TmdbClient,
    username: str,
    shelves: Sequence[Sequence[Mapping[str, object]]],
) -> list[list[dict[str, object]]]:
    """The cards of each of *shelves*, marked with what the library holds, when it leaves and
    what *username* has seen."""
    libraries = lib.scanned_library_ids(conn)
    index = catalog.library_index(conn, client, libraries, lib.library_movies, lib.library_shows)
    arr = _arr(request, conn)
    held = held_fields(conn, username, libraries)
    cards = [[catalog.tmdb_card(item, index, arr) for item in items] for items in shelves]
    return [[{**c, **held(cast(str | None, c["library_ref"]))} for c in row if c] for row in cards]


def _discover_page(
    request: Request,
    conn: sqlite3.Connection,
    client: TmdbClient,
    row: str,
    page: int,
    username: str,
) -> JSONResponse:
    try:
        # Only a page TMDB is asked for counts against the limit; a cached one is free.
        found, next_page = catalog.discover_page(
            client, row, page, allow_fetch=lambda: _PAGE_LIMITER.check(username)
        )
    except (disk_cache.Refused, catalog.TmdbBusy):
        return respond_err("too_many_requests", status=429)
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    [cards] = _discover_cards(request, conn, client, username, [found])
    marks = card_marks(seen_titles.person_marks(conn, username), client)
    items = [marked(card, marks) for card in cards]
    return respond_ok({"tmdb_configured": True, "items": items, "next_page": next_page})


def _title_is_paused(pause_key: tuple[int, str]) -> bool:
    with _pause_lock:
        now = time.monotonic()
        return now < _outage_until or now < _title_paused.get(pause_key, 0.0)


def _note_answer() -> None:
    global _unanswered_streak
    with _pause_lock:
        _unanswered_streak = 0


def _pause_title(pause_key: tuple[int, str]) -> None:
    global _unanswered_streak, _outage_until
    with _pause_lock:
        now = time.monotonic()
        if _title_paused.get(pause_key, 0.0) > now:
            return
        if len(_title_paused) >= _TITLE_PAUSE_MAX:
            for k in [k for k, until in _title_paused.items() if until <= now]:
                del _title_paused[k]
            while len(_title_paused) >= _TITLE_PAUSE_MAX:
                del _title_paused[next(iter(_title_paused))]
        _title_paused.pop(pause_key, None)
        _title_paused[pause_key] = now + _TITLE_PAUSE_SECONDS
        _unanswered_streak += 1
        if _unanswered_streak >= _OUTAGE_STREAK:
            _unanswered_streak = 0
            _outage_until = now + _OUTAGE_PAUSE_SECONDS


def _held_ratings(
    conn: sqlite3.Connection, media_type: str, tmdb_id: int
) -> tuple[dict[str, str], bool]:
    """The ratings held of a title ("movie" or "tv"), and whether they need no asking."""
    now = now_utc()
    for row in fetch_ratings_cache(conn, [(tmdb_id, media_type)]):
        cached = {"imdb": row.imdb_rating, "rt": row.rt_rating, "metascore": row.metascore}
        found = {k: v for k, v in cached.items() if v}
        ttl = _RATINGS_TTL if found else _NO_RATINGS_TTL
        return found, row.fetched_at >= (now - ttl).isoformat()
    return {}, False


def _omdb_key_to_ask(
    conn: sqlite3.Connection, secret_key: str, pause_key: tuple[int, str]
) -> str | None:
    """OMDb's key; None when it has none, was refused, or OMDb or the title is paused."""
    omdb_key = get_omdb_key(conn, secret_key)
    if not omdb_key or is_key_refused(omdb_key) or time.monotonic() < _enrichment._paused_until:
        return None
    return None if _title_is_paused(pause_key) else omdb_key


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
    none when OMDb has no key, is paused, or would be asked and *allow_fetch* says no. A title
    OMDb gave no answer for is not asked again for 15 minutes; search's whole-round pause is
    obeyed here but never set. An answer with
    nothing in it is cached too, for a shorter time, but only when OMDb answered: a timeout
    or a spent quota caches nothing and leaves the ratings already held (old ones are shown
    in its place)."""
    media_type = "movie" if kind == "movie" else "tv"
    held, fresh = _held_ratings(conn, media_type, tmdb_id)
    if fresh:
        return held
    pause_key = (tmdb_id, media_type)
    omdb_key = _omdb_key_to_ask(conn, secret_key, pause_key)
    if omdb_key is None or not allow_fetch():
        return held
    data = lookup_ratings(title, year, media_type, omdb_key=omdb_key)
    if data is None:
        _pause_title(pause_key)
        return held
    _note_answer()
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
    return {**held, **data}


def _rating_fields(ratings: Mapping[str, str], tmdb_rating: float | None) -> dict[str, Any]:
    return {
        "imdb": ratings.get("imdb"),
        "rt": ratings.get("rt"),
        "tmdb": tmdb_rating,
        "metascore": ratings.get("metascore"),
    }


def _payload(ref: str, kind: str, tmdb_configured: bool) -> dict[str, Any]:
    return {
        "ref": ref,
        "kind": kind,
        "in_library": False,
        "library_ref": None,
        **lib.mark_fields(None, set()),
        "tmdb_ref": None,
        "title": "",
        "year": None,
        "year_end": None,
        "runtime_min": None,
        "season_count": None,
        "genres": [],
        "genre_refs": [],
        "certificate": None,
        "tagline": None,
        "overview": "",
        "backdrop_url": None,
        "poster_url": None,
        "director": None,
        "director_id": None,
        "ratings": {"imdb": None, "rt": None, "tmdb": None, "metascore": None},
        "ratings_pending": False,
        "cast": [],
        "in_collection": False,
        "trailer_key": None,
        "reason": None,
        "ai_pick": False,
        "because_of": [],
        "play": None,
        "leaving": None,
        "leaving_seasons": [],
        "keep_allowed": False,
        "seasons": [],
        "episodes": [],
        "more_seasons": [],
        "seasons_pending": False,
        "also_starts": [],
        "seasons_request": None,
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
) -> dict[str, Any]:
    """Fill *out* from TMDB and the ratings held, as ``_fill_fields``; returns the TMDB
    fields. Raises TmdbUnavailable."""
    fields = catalog.details(client, kind, tmdb_id)
    _fill_fields(out, request, conn, kind, tmdb_id, fields)
    return fields


def _fill_fields(
    out: dict[str, Any],
    request: Request,
    conn: sqlite3.Connection,
    kind: str,
    tmdb_id: int,
    fields: dict[str, Any],
) -> None:
    """Fill *out* from the TMDB *fields* and the ratings held, keeping any title, overview and
    poster already set from Plex. OMDb is never waited for here: ``ratings_pending`` says that
    it is to be asked, which the title's ratings do."""
    for name in (
        "year",
        "year_end",
        "runtime_min",
        "season_count",
        "genres",
        "genre_refs",
        "certificate",
        "tagline",
        "backdrop_url",
        "director",
        "director_id",
        "cast",
    ):
        if fields[name] or out[name] in (None, []):
            out[name] = fields[name]
    for name in ("title", "overview", "poster_url"):
        out[name] = out[name] or fields[name]
    # Held details may be from before the collection was.
    out["in_collection"] = fields.get("collection_id") is not None
    out["trailer_key"] = validate_youtube_id(fields["trailer_key"])
    media_type = "movie" if kind == "movie" else "tv"
    held, fresh = _held_ratings(conn, media_type, tmdb_id)
    out["ratings"] = _rating_fields(held, fields["tmdb_rating"])
    out["ratings_pending"] = (
        not fresh
        and _omdb_key_to_ask(conn, request.app.state.config.secret_key, (tmdb_id, media_type))
        is not None
    )


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
    progress = get_progress(conn, username, [movie.key]).get(movie.key)
    seen = (progress is not None and progress.seen) or (
        movie.key in seen_titles.seen_films(conn, username, libraries)
    )
    out.update(lib.mark_fields(ref, {ref} if seen else set()))
    out["tmdb_ref"] = tmdb_ref(title_key("movie", movie.tmdb_id))
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
    out["leaving"] = leaving_info(lib.pending_deletion(conn, libraries, movie.key), now)
    out["keep_allowed"] = is_admin or out["leaving"] is not None
    if client is not None and movie.tmdb_id:
        with contextlib.suppress(catalog.TmdbUnavailable):
            _apply_tmdb(out, request, conn, client, "movie", movie.tmdb_id)
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


def _held_show_tmdb_id(conn: sqlite3.Connection, listed: lib.LibraryShow) -> int | None:
    """As ``_show_tmdb_id``, from what is held: neither Sonarr nor TMDB is asked."""
    own, _year = catalog.library_ids(conn).get(f"show-{listed.key}", (None, None))
    if own or not listed.tvdb_id:
        return own
    return catalog.held_tmdb_id_for_tvdb(listed.tvdb_id)


def _is_described(season: PlexSeason, episodes: Sequence[PlexEpisode]) -> bool:
    return bool(season.summary) and all(e.summary and e.aired for e in episodes)


def _tmdb_season(
    client: TmdbClient, tmdb_id: int, number: int, episodes: Sequence[PlexEpisode], *, wait: bool
) -> dict[str, Any] | None:
    """What TMDB says of the season, with only the episodes it has at the same number as Plex;
    None when it has none so, or no answer. Raises as ``catalog.season`` when it is not to
    *wait*."""
    try:
        found = catalog.season(client, tmdb_id, number, wait=wait)
    except catalog.TmdbUnavailable:
        return None
    if not found:
        return None
    told = found["episodes"]
    own_dates = _single([e.aired for e in episodes]) & _single([t["aired"] for t in told.values()])
    pairs = [(e, told[str(e.index)]) for e in episodes if str(e.index) in told]
    same = {str(e.index): t for e, t in pairs if _is_same_episode(e, t, own_dates)}
    if not same:
        return None
    # A season in another order still has an episode or two in their place by chance.
    compared = sum(bool((e.title and t["title"]) or (e.aired and t["aired"])) for e, t in pairs)
    about = found if len(same) * 2 > compared else {"overview": "", "aired": None}
    return {"overview": about["overview"], "aired": about["aired"], "episodes": same}


def _single(dates: Sequence[str | None]) -> set[str]:
    """The dates that stand there once."""
    return {d for d, times in Counter(dates).items() if d and times == 1}


_UNITS = "one|two|three|four|five|six|seven|eight|nine"
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
# "Episode 4", "Chapter Two", "Part III", as ``catalog.normalise_title`` leaves them.
_NUMBERED_RE = re.compile(
    rf"\w+ (?:\d+|[ivx]+|{_UNITS}|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen"
    rf"|eighteen|nineteen|(?:{_TENS})(?: (?:{_UNITS}))?)"
)


def _is_same_episode(
    episode: PlexEpisode, told: Mapping[str, Any], own_dates: Collection[str]
) -> bool:
    """Shows are ordered in more than one way: one number can be two episodes. *own_dates*: those
    of one episode alone."""
    same_date = episode.aired == told["aired"]
    if same_date and episode.aired in own_dates:
        return True
    name = catalog.normalise_title(episode.title)
    if not name or name != catalog.normalise_title(told["title"]):
        return False
    # "Episode 4" is the fourth in every order.
    numbered = _NUMBERED_RE.fullmatch(name) is not None
    return same_date or not (numbered and episode.aired and told["aired"])


def _season_about(
    season: PlexSeason, episodes: Sequence[PlexEpisode], found: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The season's own words and first year, from Plex, else from TMDB (*found*)."""
    dates = [e.aired for e in episodes if e.aired]
    aired = min(dates) if dates else found["aired"] if found else None
    return {
        "summary": season.summary or (found["overview"] if found else ""),
        "year": int(aired[:4]) if aired else None,
    }


def _described_rows(
    episodes: Sequence[PlexEpisode],
    progress: Mapping[str, WatchProgress],
    origin: str,
    found: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """The rows of a season, each with its description and air date, as ``_season_about``."""
    told = found["episodes"] if found else {}
    rows = []
    for e in episodes:
        theirs = told.get(str(e.index), {})
        rows.append(
            {
                **_episode_row(e, progress.get(e.rating_key), origin),
                "summary": e.summary or theirs.get("overview", ""),
                "aired": e.aired or theirs.get("aired"),
            }
        )
    return rows


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
    counted = lib.show_seen(conn, username, libraries, show_key=listed.key).get(listed.key)
    out.update(lib.mark_fields(ref, {ref} if counted and counted.watched else set()))
    out["tmdb_ref"] = tmdb_ref(title_key("tv", listed.tmdb_id))
    tmdb_seasons: list[dict[str, Any]] = []
    arr_view = begin_arr_view(conn, request.app.state.config.secret_key)
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

        progress = seen_titles.with_covered(
            conn, username, libraries, listed.key, show_progress(conn, username, listed.key)
        )
        primary, primary_season = _primary_episode(
            plex, show, progress, episodes_of, origin, counted
        )
        selected = next((s for s in show.seasons if s.index == primary_season), None)
        if selected is None and show.seasons:
            selected = show.seasons[0]
        episodes = episodes_of(selected.rating_key) if selected else []
    except PlexError:
        show = None
    if show is not None:
        season_watched = {
            s.rating_key: _season_watched(
                episodes_cache.get(s.rating_key), progress, counted, s.index
            )
            for s in show.seasons
        }
        out.update(
            watched=bool(season_watched) and all(season_watched.values()),
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
                    else s.episode_count,
                    "in_library": True,
                    "selected": s is selected,
                    "key": s.rating_key,
                    "watched": season_watched[s.rating_key],
                    **_season_about(s, episodes_cache.get(s.rating_key, ()), None),
                }
                for s in show.seasons
            ],
            episodes=_described_rows(episodes, progress, origin, None),
        )
    fill_show_leaving(out, conn, libraries, listed.key, is_admin)
    tmdb_id = _show_tmdb_id(request, conn, client, listed) if client is not None else None
    if client is not None and tmdb_id is not None:
        try:
            fields = _apply_tmdb(out, request, conn, client, "tv", tmdb_id)
        except catalog.TmdbUnavailable:
            pass
        else:
            counts = {s["number"]: s["episode_count"] for s in fields["tmdb_seasons"]}
            for season in out["seasons"]:
                if season["episode_count"] is None:
                    season["episode_count"] = counts.get(season["number"])
            if show is not None and selected is not None:
                found = None
                if not _is_described(selected, episodes):
                    try:
                        found = _tmdb_season(client, tmdb_id, selected.index, episodes, wait=False)
                    except disk_cache.Refused:
                        out["words_pending"] = True
                for season in out["seasons"]:
                    if season["selected"]:
                        season.update(_season_about(selected, episodes, found))
                out["episodes"] = _described_rows(episodes, progress, origin, found)
            tmdb_seasons = fields["tmdb_seasons"]
            if show is not None:
                out["more_seasons"] = missing_seasons(out["seasons"], tmdb_seasons)
    fill_season_status(
        out,
        conn,
        arr_view(),
        username,
        tmdb_id=tmdb_id,
        tvdb_id=listed.tvdb_id,
        tmdb_seasons=tmdb_seasons,
    )
    return out


def fill_show_leaving(
    out: dict[str, Any], conn: sqlite3.Connection, libraries: list[str], key: str, is_admin: bool
) -> None:
    """Each leaving season of the show, soonest first, and the first as what is leaving."""
    now = now_utc()
    seasons = [
        {**(leaving_info(p, now) or {}), "season": p.season_number}
        for p in lib.pending_deletions(conn, libraries)
        if p.show_key == key and p.season_number is not None
    ]
    out["leaving"] = seasons[0] if seasons else None
    out["leaving_seasons"] = seasons
    out["keep_allowed"] = is_admin or bool(seasons)


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
    """A TMDB title (*kind* "movie" or "tv"). Raises TmdbUnavailable, and disk_cache.Refused
    when TMDB would be asked and *allow_fetch* says no."""
    fields = catalog.details(client, kind, tmdb_id, allow_fetch=allow_fetch)
    return _title_of_fields(
        request, conn, username, client, ref, kind, tmdb_id, download_url, fields
    )


def _title_of_fields(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    client: TmdbClient | None,
    ref: str,
    kind: str,
    tmdb_id: int,
    download_url: str | None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    out = _payload(ref, "movie" if kind == "movie" else "show", client is not None)
    _fill_fields(out, request, conn, kind, tmdb_id, fields)
    # A slow Radarr or Sonarr does not hold the page: its download state comes on a later answer.
    arr = begin_arr_view(conn, request.app.state.config.secret_key)()
    index = catalog.library_index(
        conn, client, lib.scanned_library_ids(conn), lib.library_movies, lib.library_shows
    )
    library_ref = index.library_ref(
        kind, tmdb_id, fields["title"], arr, year=fields["year"], tvdb_id=fields["tvdb_id"]
    )
    if library_ref is not None:
        seen = lib.seen_refs(conn, username, lib.scanned_library_ids(conn))
        out.update(in_library=True, library_ref=library_ref, **lib.mark_fields(library_ref, seen))
    own = seen_titles.person_marks(conn, username)
    title = (kind, tmdb_id)
    out["tmdb_ref"] = tmdb_ref(title)
    if title in own.seen and library_ref is None:
        latest = latest_season_out(fields, now_utc().date()) if kind == "tv" else None
        out["watched"] = title not in own.partly and (
            latest is None or latest <= (own.seen[title] or 0)
        )
    if kind == "tv":
        held = arr.seasons_with_files(tmdb_id)
        out["seasons"] = [
            {
                **s,
                "in_library": s["number"] in held,
                "selected": False,
                "key": None,
                "watched": False,
            }
            for s in fields["tmdb_seasons"]
        ]
        fill_season_status(
            out,
            conn,
            arr,
            username,
            tmdb_id=tmdb_id,
            tvdb_id=fields["tvdb_id"],
            tmdb_seasons=fields["tmdb_seasons"],
            download_url=download_url,
        )
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
                username=username,
                service=service,
                tmdb_id=tmdb_id,
                tvdb_id=(fields["tvdb_id"] or arr.tvdb_id(tmdb_id)) if kind == "tv" else None,
            ),
            "service": service,
            "tmdb_id": tmdb_id,
            "request": request_body,
        }
    return out


def _held_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    is_admin: bool,
    libraries: list[str],
    ref: str,
) -> dict[str, Any] | None:
    """The library title *ref*; None when it is not in the scanned libraries."""
    listed = _library_title(conn, libraries, ref)
    if listed is None:
        return None
    try:
        if isinstance(listed, lib.LibraryMovie):
            return _movie_title(request, conn, username, is_admin, libraries, listed)
        return _show_title(request, conn, username, is_admin, libraries, listed)
    except _OutsideLibraries:
        return None


@router.get("/api/watch/title/{ref}")
def api_watch_title(
    ref: str, request: Request, ahead: bool = False, username: str = Depends(get_current_user)
) -> JSONResponse:
    if not REF_RE.fullmatch(ref):
        return _not_found()
    if ahead and not _AHEAD_LIMITER.check(username):
        return respond_err("too_many_requests", status=429)
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    is_admin = get_user_role(conn, username) == "admin"
    kind, _, rest = ref.partition("-")

    def held(library_ref: str) -> dict[str, Any] | None:
        return _held_title(request, conn, username, is_admin, libraries, library_ref)

    def without_tmdb(otherwise: JSONResponse) -> JSONResponse:
        """The library's own copy of the TMDB title asked for; *otherwise* when it has none."""
        if kind != "tmdb":
            return otherwise
        media_type, _, tmdb_id = rest.partition("-")
        for copy in seen_titles.library_refs(conn, libraries, (media_type, int(tmdb_id))):
            if (title := held(copy)) is not None:
                return respond_ok(title)
        return otherwise

    if kind in ("movie", "show"):
        title = held(ref)
        return _not_found() if title is None else respond_ok(title)

    client = _tmdb_client(request, conn)
    try:
        if kind != "tmdb":
            rec = _recommended(conn, username, kind, rest)
            if rec is None:
                return _not_found()
            return respond_ok(_recommended_title(request, conn, username, client, ref, kind, rec))
        if client is None:
            return without_tmdb(respond_err("tmdb_not_configured", status=503))
        media_type, _, tmdb_id = rest.partition("-")
        # Any id can be asked for, so only a title TMDB must answer counts against the limit;
        # a cached one is free.
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
                lambda: _QUERY_LIMITER.check(username),
            )
        )
    except disk_cache.Refused:
        return respond_err("too_many_requests", status=429)
    except catalog.TmdbBusy:
        return without_tmdb(respond_err("too_many_requests", status=429))
    except catalog.TitleNotFound:
        return _not_found()
    except catalog.TmdbUnavailable:
        return without_tmdb(respond_err("tmdb_request_failed", status=502))


def _recommended(
    conn: sqlite3.Connection, username: str, kind: str, key: str
) -> SuggestionDetail | None:
    rec = (
        fetch_pick(conn, username, int(key))
        if kind == "pick"
        else fetch_suggestion_by_id(conn, int(key))
    )
    return rec if rec is not None and rec.media_type in ("movie", "tv") else None


def _recommended_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    client: TmdbClient | None,
    ref: str,
    kind: str,
    rec: SuggestionDetail,
) -> dict[str, Any]:
    """A recommendation (*kind* "pick" or "rec"): TMDB's title, or what was stored of it when
    TMDB has no id for it or gives no answer. Raises TitleNotFound."""
    download_url = (
        f"/api/watch/picks/{rec.id}/download"
        if kind == "pick"
        else f"/api/recommended/{rec.id}/download"
    )
    out: dict[str, Any] | None = None
    if rec.tmdb_id and client is not None:
        try:
            out = _tmdb_title(
                request, conn, username, client, ref, rec.media_type, rec.tmdb_id, download_url
            )
        except catalog.TitleNotFound:
            raise
        except catalog.TmdbUnavailable:
            pass  # What was stored is better than none.
    if out is None:
        out = _stored_title(request, conn, username, client, ref, rec, download_url)
    out.update(reason=rec.reason or None, ai_pick=True, because_of=list(rec.because_of))
    return out


def _stored_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    client: TmdbClient | None,
    ref: str,
    rec: SuggestionDetail,
    download_url: str,
) -> dict[str, Any]:
    fields = {
        **catalog.tmdb_fields({}, rec.media_type),
        "title": rec.title,
        "year": rec.year,
        "season_count": None,
        "overview": tidy_summary(rec.description) or "",
        "poster_url": catalog.tmdb_image_url(rec.poster_url, "w500"),
        "tmdb_rating": rec.rating,
    }
    if rec.tmdb_id:
        out = _title_of_fields(
            request, conn, username, client, ref, rec.media_type, rec.tmdb_id, download_url, fields
        )
    else:
        out = _payload(ref, "movie" if rec.media_type == "movie" else "show", client is not None)
        for name in ("title", "year", "overview", "poster_url"):
            out[name] = fields[name]
        out["ratings"]["tmdb"] = rec.rating
    out["ratings"]["rt"] = out["ratings"]["rt"] or rec.rt_rating
    # The title's ratings are asked for by TMDB's words of it.
    out["ratings_pending"] = False
    return out


@router.get("/api/watch/title/{ref}/ratings")
def api_watch_title_ratings(
    ref: str, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """A title's ratings, with OMDb asked for those not held: what the title leaves to be asked
    after it, as OMDb can be slow to answer."""
    if not REF_RE.fullmatch(ref):
        return _not_found()
    conn = get_db()
    client = _tmdb_client(request, conn)
    if client is None:
        return respond_err("tmdb_not_configured", status=503)
    found = _rated(request, conn, username, client, ref)
    if found is None:
        return _not_found()
    media_type, tmdb_id, listed = found
    # Counted once, whatever TMDB and OMDb must be asked; what is held is free.
    allowed: list[bool] = []

    def allow_fetch() -> bool:
        if not allowed:
            allowed.append(_RATINGS_LIMITER.check(username))
        return allowed[0]

    try:
        fields = catalog.details(client, media_type, tmdb_id, allow_fetch=allow_fetch)
    except (disk_cache.Refused, catalog.TmdbBusy):
        return respond_err("too_many_requests", status=429)
    except catalog.TitleNotFound:
        return _not_found()
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    ratings = _ratings(
        conn,
        request.app.state.config.secret_key,
        media_type,
        tmdb_id,
        listed.title if listed else fields["title"],
        fields["year"] or (listed.year if listed else None),
        allow_fetch,
    )
    return respond_ok({"ratings": _rating_fields(ratings, fields["tmdb_rating"])})


def _is_outside(
    request: Request, conn: sqlite3.Connection, listed: lib.LibraryMovie | lib.LibraryShow
) -> bool:
    """Whether Plex, when it answers, places a listed title outside the scanned libraries."""
    libraries = lib.scanned_library_ids(conn)
    try:
        plex = open_plex(request, conn)
        if isinstance(listed, lib.LibraryMovie):
            item = plex.get_item(listed.key)
            return item.type != "movie" or not is_in_libraries(item, libraries)
        return not is_in_libraries(plex.get_show(listed.key), libraries)
    except PlexError:
        return False


def _rated(
    request: Request, conn: sqlite3.Connection, username: str, client: TmdbClient, ref: str
) -> tuple[str, int, lib.LibraryMovie | lib.LibraryShow | None] | None:
    """The kind ("movie" or "tv") and TMDB id of the title *ref* names, and the library title
    it is when the ref is of one; None when there is no such title or TMDB id."""
    kind, _, rest = ref.partition("-")
    if kind in ("movie", "show"):
        listed = _library_title(conn, lib.scanned_library_ids(conn), ref)
        if listed is None or _is_outside(request, conn, listed):
            return None
        if isinstance(listed, lib.LibraryMovie):
            return ("movie", listed.tmdb_id, listed) if listed.tmdb_id else None
        show_id = _show_tmdb_id(request, conn, client, listed)
        return ("tv", show_id, listed) if show_id else None
    if kind == "tmdb":
        media_type, _, tmdb_id = rest.partition("-")
        return media_type, int(tmdb_id), None
    rec = _recommended(conn, username, kind, rest)
    return (rec.media_type, rec.tmdb_id, None) if rec and rec.tmdb_id else None


@router.get("/api/watch/title/{ref}/collection")
def api_watch_title_collection(
    ref: str, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """The films of the collection that film *ref* is part of, ``here`` marking its own, and
    why one is not to be asked for; null when there is nothing to show."""
    if not REF_RE.fullmatch(ref):
        return _not_found()
    nothing = JSONResponse(None)
    if ref.startswith(("show-", "tmdb-tv-")):
        return nothing
    conn = get_db()
    client = _tmdb_client(request, conn)
    found = _rated(request, conn, username, client, ref) if client is not None else None
    if client is None or found is None or found[0] != "movie":
        return nothing
    tmdb_id = found[1]
    # Counted once, though TMDB may be asked for the film and for its collection.
    allowed: list[bool] = []

    def allow_fetch() -> bool:
        if not allowed:
            allowed.append(_COLLECTION_LIMITER.check(username))
        return allowed[0]

    try:
        collection = catalog.film_collection(client, tmdb_id, allow_fetch=allow_fetch)
    except (disk_cache.Refused, catalog.TmdbBusy):
        return respond_err("too_many_requests", status=429)
    except catalog.TitleNotFound:
        return nothing
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    if collection is None or len(collection["films"]) < 2:
        return nothing
    films = collection["films"]
    [found_cards] = _discover_cards(request, conn, client, username, [films])
    marks = card_marks(seen_titles.person_marks(conn, username), client)
    cards = [marked(card, marks) for card in found_cards]
    answered = _arr(request, conn).answers_for("movie")
    shown = []
    for film, card in zip(films, cards, strict=True):
        why_not = _why_not_to_request(card, film["release_date"], answered)
        shown.append(
            {
                **card,
                "release_date": film["release_date"],
                "here": film["id"] == tmdb_id,
                "can_request": why_not is None,
                "why_not": why_not,
            }
        )
    return JSONResponse({"name": collection["name"], "films": shown})


def _why_not_to_request(
    card: Mapping[str, object], release_date: str | None, radarr_answered: bool
) -> str | None:
    state = card["download_state"]
    if card["in_library"] or state in (ACTION_IN_LIBRARY, ACTION_PARTIAL):
        return "held"
    if state is not None:
        return "requested"
    if release_date is None:
        return "undated"
    if release_date > now_utc().date().isoformat():
        return "unreleased"
    return None if radarr_answered else "unavailable"


@router.get("/api/watch/title/{ref}/season/{number}")
def api_watch_title_season(
    ref: str,
    number: int,
    request: Request,
    words: bool = False,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """The episodes of one season of a library show, with this person's progress. TMDB is waited
    for only when asked for its *words* too; else ``words_pending`` says that it may have some."""
    if not REF_RE.fullmatch(ref) or not ref.startswith("show-") or not 0 <= number <= 9999:
        return _not_found()
    key = ref.removeprefix("show-")
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    listed = lib.find_show(conn, libraries, key)
    if listed is None:
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
    origin = f"/watch#title={ref}"
    client = _tmdb_client(request, conn)
    found, pending = None, False
    if client is not None and not _is_described(season, episodes):
        tmdb_id = (
            _show_tmdb_id(request, conn, client, listed)
            if words
            else _held_show_tmdb_id(conn, listed)
        )
        # Who knows a show by its TVDB id alone is yet to be asked.
        pending = tmdb_id is None and not words and bool(listed.tvdb_id)
        if tmdb_id is not None:
            try:
                found = _tmdb_season(client, tmdb_id, number, episodes, wait=words)
            except disk_cache.Refused:
                pending = not words
    # What was seen or marked during the wait counts.
    progress = seen_titles.with_covered(
        conn, username, libraries, key, show_progress(conn, username, key)
    )
    return respond_ok(
        {
            "key": season.rating_key,
            "watched": _season_watched(episodes, progress, None, number),
            **_season_about(season, episodes, found),
            "episode_count": len(episodes),
            "episodes": _described_rows(episodes, progress, origin, found),
            "words_pending": pending,
        }
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


def _deletions(
    conn: sqlite3.Connection, libraries: list[str], facts: lib.TitleFacts, now: datetime
) -> list[dict[str, Any]]:
    """What of the title is scheduled for deletion, soonest first."""
    ids = set(facts.item_ids)
    out: list[dict[str, Any]] = []
    for pending in lib.pending_deletions(conn, libraries):
        if pending.media_item_id not in ids:
            continue
        execute_at = parse_execute_at(pending.execute_at, default=now)
        out.append(
            {
                "keep_id": pending.media_item_id,
                "season": pending.season_number,
                "execute_at": execute_at.isoformat(),
                "days": days_left(execute_at, now),
            }
        )
    return out


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
    # Read before what the page shows, so a keep made between the two is never taken for the one shown.
    read = fetch_keep(conn, key) if kind == "movie" else None
    facts = lib.title_facts(conn, libraries, kind, key, now)
    if facts is None:
        return _not_found()
    keep = _keep(facts)
    deletions = _deletions(conn, libraries, facts, now)
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
            "deletion": deletions[0] if deletions else None,
            "deletions": deletions,
            "keep": keep,
            "actions": {
                "keep_wrapper": {
                    "id": facts.item_ids[0],
                    "is_tv": is_show,
                    "show_rk": key if is_show else "",
                    "has_rk": is_show,
                    "show_title": facts.title,
                    "keep": asdict(read) if read else None,
                    "seasons": len(facts.item_ids) if is_show else None,
                    "has_keeps": facts.show_keep is not None or bool(facts.item_keeps),
                },
                "delete": delete,
            },
        }
    )


__all__ = ["REF_RE", "router"]
