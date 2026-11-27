"""Watch next: the endless section at the end of Watch, one page of 24 titles at a time.

It mixes the AI's stored picks, TMDB's recommendations for what the person played, their
searches, TMDB's trending list, titles of the genres they like and, for someone it knows
nothing of, trending and popular titles. Nothing asks the AI: the picks are those it already
made. TMDB is read through the same caches as the discover rows, and a source that fails is
passed over for the page. No title comes twice in one cursor's pages, none of the first page of
a row on Watch, and none the person marked watched or rated down.
"""

from __future__ import annotations

import functools
import hashlib
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.crypto import generate_watch_next_cursor, validate_watch_next_cursor
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import disk_cache, get_bool_setting
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai.recommendations.plays import PlayedTitle, person_plays
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.search_taste import search_taste
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import asked
from mediaman.web.repository import search_assist as assist_repo
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.recommended import fetch_recommendations, group_into_batches
from mediaman.web.repository.seen_titles import PersonMarks, person_marks
from mediaman.web.repository.watch_progress import continue_watching
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.card_marks import card_marks, marked, scored
from mediaman.web.routes.watch.catalog_api import _PAGE_LIMITER, _arr, _tmdb_client, held_fields
from mediaman.web.routes.watch.next_mix import MAX_SEEDS, Bloom, Mixer, MixState, Seed, Source
from mediaman.web.routes.watch.pages import _CONTINUE_LIMIT, ROW_PAGE_SIZE, recently_added
from mediaman.web.routes.watch.recommended_api import _attach_states, _card

router = APIRouter()

PAGE_SIZE = 24
_TYPES = ("all", "movie", "tv")
WEIGHTS = {"ai": 3, "watched": 3, "trending": 3, "search": 2, "taste": 2, "popular": 0}
NEW_WEIGHTS = {"trending": 1, "popular": 1}
_KINDS = tuple(WEIGHTS)
_CURSOR_TTL_S = 7 * 24 * 60 * 60
# A page reads at most this many pages of its sources, cached or not, and asks TMDB at most
# this many times; what is left waits for the next page.
_READS = 40
_ASKS = 6

# One page every few seconds is a fast scroll; the TMDB pages behind them count apart, in the
# discover rows' limit.
_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60, max_per_day=3000)

# What the rows above show and what the person played, held a short while per person: each
# page of a scroll would otherwise read them again.
_HELD_S = 120.0
_HELD_MAX = 256
_held: dict[tuple[str, str], tuple[float, _Known]] = {}
_held_lock = threading.Lock()

_FAILURES = (catalog.TmdbUnavailable, disk_cache.Refused)


@dataclass(frozen=True, slots=True)
class _Known:
    above: frozenset[str]
    plays: tuple[PlayedTitle, ...]


@router.get("/api/watch/next")
def api_watch_next(
    request: Request,
    type: str = "all",
    cursor: str | None = None,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """24 cards, each as on a discover row (a stored AI pick as on the Recommended row) with
    ``reason``: ``kind`` (ai, watched, search, trending, taste or popular), ``text``, ``short``
    for a narrow card and ``why``, said in full. ``cursor`` asks for the page after; one that
    is not this server's, this person's or this *type*'s starts again from the head. Fewer
    than 24 come back only when the sources had no more to give this time. ``personal`` is
    False for someone with no plays, ratings, marks or searches."""
    if type not in _TYPES:
        return respond_err("invalid_request", status=400)
    if not _LIMITER.check(username):
        return respond_err("too_many_requests", status=429)
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    client = _tmdb_client(request, conn)
    libraries = lib.scanned_library_ids(conn)
    index = catalog.library_index(conn, client, libraries, lib.library_movies, lib.library_shows)
    now = now_utc()
    known = _known(conn, username, client, libraries, index, now)
    marks = person_marks(conn, username)
    user_id = assist_repo.user_id_of(conn, username)
    searches = (
        assist_repo.recent_searches_at(conn, user_id, MAX_SEEDS * 2)
        if user_id is not None and assist_repo.uses_history(conn, user_id)
        else []
    )
    personal = bool(known.plays or marks.levels or marks.seen or searches)

    who = hashlib.sha256(username.encode()).hexdigest()[:16]
    state, bloom = _read_cursor(cursor, secret_key, who, type)
    above = set(known.above)
    arr = _arr(request, conn)
    build = _Cards(
        conn, username, index, arr, held_fields(conn, username, libraries), marks, client
    )
    ask = _asker(username)

    sources: list[Source] = []
    if personal:
        sources.append(_ai_source(conn, username, marks, build, above, now.date()))
    if client is not None:
        if personal:
            sources += [
                _watched_source(client, known.plays, type, build, ask),
                _search_source(client, searches, index, build, ask, now),
                _taste_source(conn, username, user_id, client, type, build, ask, now),
            ]
        sources += [_trending_source(client, build, ask), _popular_source(client, type, build, ask)]
    weights = WEIGHTS if personal else NEW_WEIGHTS
    sources = [
        Source(s.kind, weights.get(s.kind, 0), s.seeds) for s in sources if s.kind in weights
    ]

    def accept(item: Any, reason: Mapping[str, str]) -> dict[str, Any] | None:
        card = cast(Callable[[], dict[str, Any] | None], item)()
        if card is None or not _shown(card, type):
            return None
        keys = _keys(card)
        if any(k in above or k in bloom for k in keys):
            return None
        for k in keys:
            bloom.add(k)
        said = dict(reason)
        # A stored pick says why in its own words.
        if isinstance(card.get("reason"), str) and card["reason"]:
            said["why"] = card["reason"]
        return {**card, "reason": said}

    mixer = Mixer(sources, state, accept, _FAILURES, _READS)
    items = scored(conn, mixer.take(PAGE_SIZE))
    following = {"v": 1, "u": who, "t": type, "m": mixer.state().as_json(), "b": bloom.text()}
    return respond_ok(
        {
            "items": items,
            "cursor": generate_watch_next_cursor(
                state=following, ttl_s=_CURSOR_TTL_S, secret_key=secret_key
            ),
            "personal": personal,
            "tmdb_configured": client is not None,
        }
    )


def _read_cursor(
    cursor: str | None, secret_key: str, who: str, type_: str
) -> tuple[MixState, Bloom]:
    """The state a cursor holds; a fresh one for none, or one that is not as this page wrote it."""
    payload = validate_watch_next_cursor(cursor, secret_key) if cursor else None
    st: Mapping[str, object] = payload["st"] if payload else {}
    if st.get("v") == 1 and st.get("u") == who and st.get("t") == type_:
        mix = st.get("m")
        state = MixState.read(mix, _KINDS) if isinstance(mix, dict) else None
        bloom = Bloom.read(st.get("b"))
        if state is not None and bloom is not None:
            return state, bloom
    return MixState(), Bloom()


def _asker(username: str) -> Callable[[], bool]:
    left = [_ASKS]

    def ask() -> bool:
        if left[0] <= 0:
            return False
        left[0] -= 1
        return _PAGE_LIMITER.check(username)

    return ask


# --- Cards ---------------------------------------------------------------------------


class _Cards:
    """The cards of TMDB titles and of stored picks, as the discover and Recommended rows
    make them."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        username: str,
        index: catalog.LibraryIndex,
        arr: catalog.ArrView,
        held: Callable[[str | None], dict[str, object]],
        marks: PersonMarks,
        client: TmdbClient | None,
    ) -> None:
        self._conn, self._username, self._index, self._arr = conn, username, index, arr
        self._held = held
        self._marks = card_marks(marks, client)

    def tmdb(self, item: Mapping[str, Any]) -> dict[str, Any] | None:
        card = catalog.tmdb_card(item, self._index, self._arr)
        if card is None:
            return None
        return marked({**card, **self._held(cast(str | None, card["library_ref"]))}, self._marks)

    def pick(self, row: dict[str, object]) -> dict[str, Any]:
        _attach_states(self._conn, [row], self._arr, self._username)
        return _card(row, self._index, self._arr, self._held, self._marks)

    def lazy_tmdb(self, items: Sequence[Mapping[str, Any]]) -> list[Callable[[], Any]]:
        return [functools.partial(self.tmdb, item) for item in items]


def _shown(card: Mapping[str, Any], type_: str) -> bool:
    if type_ != "all" and (card["kind"] == "movie") != (type_ == "movie"):
        return False
    if card.get("watched"):
        return False
    level = card.get("level")
    return not (isinstance(level, int) and level < 0)


def _title_key(kind: str, title: str, year: object) -> str:
    return f"{kind}|{catalog.normalise_title(title)}|{year if isinstance(year, int) else ''}"


def _keys(card: Mapping[str, Any]) -> list[str]:
    """A card's names in the set of what was shown: its TMDB title, or without one its title,
    kind and year, as the page's own check of its grid does; and its library title."""
    tmdb_ref = card.get("tmdb_ref")
    if isinstance(tmdb_ref, str):
        _, kind, tmdb_id = tmdb_ref.split("-")
        keys = [f"{kind}:{tmdb_id}"]
    else:
        kind = "movie" if card["kind"] == "movie" else "tv"
        keys = [_title_key(kind, str(card.get("title") or ""), card.get("year"))]
    ref = card.get("library_ref")
    return keys + ([f"ref:{ref}"] if isinstance(ref, str) else [])


# --- What the rows above show --------------------------------------------------------


def _known(
    conn: sqlite3.Connection,
    username: str,
    client: TmdbClient | None,
    libraries: list[str],
    index: catalog.LibraryIndex,
    now: datetime,
) -> _Known:
    key = (str(conn.execute("PRAGMA database_list").fetchone()[2]), username)
    with _held_lock:
        held = _held.get(key)
    if held is not None and time.monotonic() - held[0] < _HELD_S:
        return held[1]
    plays = tuple(p for p in person_plays(conn, username) if p.tmdb_id)[:MAX_SEEDS]
    found = _Known(_rows_above(conn, username, client, libraries, index, now), plays)
    with _held_lock:
        if len(_held) >= _HELD_MAX:
            _held.clear()
        _held[key] = (time.monotonic(), found)
    return found


def _rows_above(
    conn: sqlite3.Connection,
    username: str,
    client: TmdbClient | None,
    libraries: list[str],
    index: catalog.LibraryIndex,
    now: datetime,
) -> frozenset[str]:
    """The names, as ``_keys`` gives them, of the titles on the first page of each row of
    Watch: Continue watching, Leaving soon, Recently added, the library's rows, what the
    person asked for and the discover rows. The Recommended row's are added with the picks."""
    keys: set[str] = set()

    def library(ref: str) -> None:
        keys.add(f"ref:{ref}")
        tmdb_id = index.tmdb_of.get(ref)
        if tmdb_id:
            keys.add(f"{'movie' if ref.startswith('movie-') else 'tv'}:{tmdb_id}")

    movies = lib.newest_first(lib.library_movies(conn, libraries))
    shows = lib.newest_first(lib.library_shows(conn, libraries))
    rows: list[Sequence[lib.LibraryMovie | lib.LibraryShow]] = [
        movies,
        [s for s in shows if not s.anime],
        [s for s in shows if s.anime],
        recently_added(lib.newest_first([*movies, *shows]), now),
    ]
    for row in rows:
        for title in row[:ROW_PAGE_SIZE]:
            library(f"{'movie' if isinstance(title, lib.LibraryMovie) else 'show'}-{title.key}")
    for entry in continue_watching(conn, username)[:_CONTINUE_LIMIT]:
        progress = entry.progress
        show = progress.show_rating_key
        library(f"show-{show}" if show is not None else f"movie-{progress.rating_key}")
    for pending in lib.pending_deletions(conn, libraries):
        if pending.season_number is None:
            library(f"movie-{pending.media_item_id}")
        elif pending.show_key is not None:
            library(f"show-{pending.show_key}")
    for request in asked.requests_of(conn, username):
        if request.kind is not None and request.tmdb_id:
            keys.add(f"{request.kind}:{request.tmdb_id}")
    if client is not None:
        try:
            shelves = catalog.discover_shelves(client)
        except catalog.TmdbUnavailable:
            shelves = []
        for _, _, items in shelves:
            for item in items:
                if item.get("media_type") in ("movie", "tv") and isinstance(item.get("id"), int):
                    keys.add(f"{item['media_type']}:{item['id']}")
    return frozenset(keys)


# --- The sources ---------------------------------------------------------------------


def _reason(kind: str, text: str, short: str, why: str) -> dict[str, str]:
    return {"kind": kind, "text": text, "short": short, "why": why}


def _ai_source(
    conn: sqlite3.Connection,
    username: str,
    marks: PersonMarks,
    build: _Cards,
    above: set[str],
    today: date,
) -> Source:
    """The picks of the batches the Recommended row is not showing, newest first. Those of
    the one it shows join *above*."""
    if not get_bool_setting(conn, "suggestions_enabled", default=True):
        return Source("ai", 0, [])
    batches = group_into_batches(
        fetch_recommendations(conn, username), today, marks.levels, marks.seen
    )
    shown = next((place for place, b in enumerate(batches) if b["items"]), len(batches))
    rows: list[dict[str, object]] = []
    for place, batch in enumerate(batches[shown:]):
        for row in cast(list[dict[str, object]], batch["items"]):
            if place == 0:
                above.update(_row_keys(row))
            else:
                rows.append(row)
    if not rows:
        return Source("ai", 0, [])
    thunks = [functools.partial(build.pick, row) for row in rows]
    why = "Picked for you by the AI, from what you watch."
    reason = _reason("ai", "AI pick", "AI pick", why)
    return Source("ai", 0, [Seed(reason, lambda _page: (thunks, None))])


def _row_keys(row: Mapping[str, object]) -> list[str]:
    kind = "movie" if row.get("media_type") == "movie" else "tv"
    tmdb_id = row.get("tmdb_id")
    if isinstance(tmdb_id, int) and tmdb_id > 0:
        return [f"{kind}:{tmdb_id}"]
    return [_title_key(kind, str(row.get("title") or ""), row.get("year"))]


def _watched_source(
    client: TmdbClient,
    plays: Sequence[PlayedTitle],
    type_: str,
    build: _Cards,
    ask: Callable[[], bool],
) -> Source:
    """TMDB's recommendations for each title played, the latest first."""
    seeds = []
    for play in plays:
        if play.tmdb_id is None or (type_ != "all" and play.kind != type_):
            continue
        reason = _reason(
            "watched",
            f"Because you watched {play.title}",
            f"Like {play.title}",
            f"People who watched {play.title} went on to this.",
        )
        seeds.append(Seed(reason, _similar_read(client, play.kind, play.tmdb_id, build, ask)))
    return Source("watched", 0, seeds)


def _similar_read(
    client: TmdbClient, kind: str, tmdb_id: int, build: _Cards, ask: Callable[[], bool]
) -> Callable[[int], tuple[Sequence[Any], int | None]]:
    def read(_page: int) -> tuple[Sequence[Any], int | None]:
        return build.lazy_tmdb(catalog.similar(client, kind, tmdb_id, allow_fetch=ask)), None

    return read


def _search_source(
    client: TmdbClient,
    searches: Sequence[tuple[str, str]],
    index: catalog.LibraryIndex,
    build: _Cards,
    ask: Callable[[], bool],
    now: datetime,
) -> Source:
    """TMDB's titles for each of the person's latest searches. One that named a library
    title as it is was a look-up, not a taste, and is left out."""
    seeds = []
    for phrase, at in searches:
        name = catalog.normalise_title(phrase)
        if not name or name in index.movies_by_title or name in index.shows_by_title:
            continue
        quoted = f"“{phrase}”"
        reason = _reason(
            "search",
            f"Because you searched {quoted}",
            quoted,
            f"You searched {quoted} {_when(at, now)}.",
        )
        seeds.append(Seed(reason, _search_read(client, phrase, build, ask)))
        if len(seeds) == MAX_SEEDS:
            break
    return Source("search", 0, seeds)


def _search_read(
    client: TmdbClient, phrase: str, build: _Cards, ask: Callable[[], bool]
) -> Callable[[int], tuple[Sequence[Any], int | None]]:
    def read(_page: int) -> tuple[Sequence[Any], int | None]:
        return build.lazy_tmdb(catalog.search(client, phrase, allow_fetch=ask)), None

    return read


def _when(at: str, now: datetime) -> str:
    when = parse_iso_strict_utc(at)
    if when is None:
        return "lately"
    days = (now.date() - when.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"on {when:%A}"
    return f"on {when.day} {when:%b}"


def _taste_source(
    conn: sqlite3.Connection,
    username: str,
    user_id: int | None,
    client: TmdbClient,
    type_: str,
    build: _Cards,
    ask: Callable[[], bool],
    now: datetime,
) -> Source:
    """TMDB's popular titles of each genre the person watches and rates most."""
    if user_id is None:
        return Source("taste", 0, [])
    taste = search_taste(conn, username, user_id, now)
    genres = [
        catalog.GENRES[g]
        for g in taste["favourites"]
        if g in catalog.GENRES and (type_ == "all" or type_ in catalog.GENRES[g].types)
    ]
    names = ", ".join(g.name for g in genres)
    reason = _reason(
        "taste",
        "More like your taste",
        "Your taste",
        f"From what you watch and rate highly: {names}.",
    )
    return Source(
        "taste", 0, [Seed(reason, _genre_read(client, g, type_, build, ask)) for g in genres]
    )


def _genre_read(
    client: TmdbClient,
    genre: catalog.Genre,
    type_: str,
    build: _Cards,
    ask: Callable[[], bool],
) -> Callable[[int], tuple[Sequence[Any], int | None]]:
    def read(page: int) -> tuple[Sequence[Any], int | None]:
        items, _, following = catalog.explore_page(
            client, genre, media_type=type_, page=page, allow_fetch=ask
        )
        return build.lazy_tmdb(items), following if isinstance(following, int) else None

    return read


def _list_read(
    client: TmdbClient, row: str, build: _Cards, ask: Callable[[], bool]
) -> Callable[[int], tuple[Sequence[Any], int | None]]:
    def read(page: int) -> tuple[Sequence[Any], int | None]:
        items, following = catalog.discover_page(client, row, page, allow_fetch=ask)
        return build.lazy_tmdb(items), following

    return read


def _trending_source(client: TmdbClient, build: _Cards, ask: Callable[[], bool]) -> Source:
    """TMDB's trending titles of the week, from its second page: the first is a row above."""
    reason = _reason(
        "trending",
        "Trending this week",
        "Trending",
        "One of the most watched titles on TMDB this week.",
    )
    return Source("trending", 0, [Seed(reason, _list_read(client, "trending", build, ask), 2)])


def _popular_source(
    client: TmdbClient, type_: str, build: _Cards, ask: Callable[[], bool]
) -> Source:
    """TMDB's popular films and TV in turn, from their second pages."""
    reason = _reason(
        "popular",
        "Popular right now",
        "Popular",
        "Popular with everyone right now, not chosen from your history.",
    )
    rows = [
        row
        for row, kind in (("popular_movies", "movie"), ("popular_tv", "tv"))
        if type_ in ("all", kind)
    ]
    return Source("popular", 0, [Seed(reason, _list_read(client, r, build, ask), 2) for r in rows])


__all__ = ["router"]
