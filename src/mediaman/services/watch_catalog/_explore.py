"""TMDB titles by genre, and people with everything they worked on, for Watch. Cached as
``_discover`` caches: TMDB's own answers only, and of those only what is shown. Nothing
TMDB marks adult is kept."""

from __future__ import annotations

import functools
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

import requests

from mediaman.core.time import now_utc
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient, dicts, vote_of

from . import _discover
from ._discover import (
    _CARD_FIELDS,
    _DAY,
    _HOUR,
    MAX_PAGE,
    _cached,
    _failure,
    _is_gone,
    _noted,
)
from ._gate import TmdbUnavailable, gate
from ._genres import GENRES, Genre
from ._shape import tmdb_id_of, tmdb_image_url, tmdb_kind

TYPES = ("all", "movie", "tv")
SORTS = ("popular", "top_rated", "newest")
_FIRST_DECADE = 1930
# As many as a page of TMDB's, and of those the most that one is made from: two of each
# kind where they come evenly, and some that hold next to nothing.
_MERGED_PAGE = 20
_MERGED_READS = 6
# The titles a place names: all of a page of TMDB's.
_PLACE_MAX = 20

# Below these an average is a handful of people's.
_TOP_RATED_VOTES = {"movie": 300, "tv": 200}
_NEWEST_VOTES = 10

_TILE_POSTERS = 2
# A list is asked for no more often than this, also when the cache cannot keep it.
_TILES_PAUSE_S = 10 * 60.0
_clock = time.monotonic
_tiles_asked: dict[str, float] = {}

PEOPLE_LIMIT = 3
KNOWN_FOR_LIMIT = 4
BIOGRAPHY_MAX = 600
_CREDITS_MAX = 300
_ROLE_MAX = 120
# TMDB's TV genres of people appearing as themselves.
_AS_THEMSELVES_GENRES = frozenset({10763, 10764, 10767})
_AS_THEMSELVES = re.compile(r"(self|himself|herself|themselves)\b", re.IGNORECASE)

_JOBS = {
    "Acting": "Actor",
    "Directing": "Director",
    "Writing": "Writer",
    "Production": "Producer",
    "Creator": "Creator",
    "Editing": "Editor",
    "Camera": "Camera",
    "Sound": "Sound and music",
    "Art": "Art",
    "Costume & Make-Up": "Costume and make-up",
    "Visual Effects": "Visual effects",
    "Crew": "Crew",
}


class PersonNotFound(TmdbUnavailable):
    """TMDB has no such person, or marks them adult."""


def decades() -> list[str]:
    """Newest first."""
    current = now_utc().year // 10 * 10
    return [f"{year}s" for year in range(current, _FIRST_DECADE - 1, -10)]


def _discover_params(
    media_type: str, genre_id: int, sort: str, decade: str | None, page: int
) -> dict[str, object]:
    released = "primary_release_date" if media_type == "movie" else "first_air_date"
    params: dict[str, object] = {"with_genres": genre_id, "page": page}
    until = None
    if decade is not None:
        params[f"{released}.gte"] = f"{decade[:4]}-01-01"
        until = f"{decade[:3]}9-12-31"
    if sort == "popular":
        params["sort_by"] = "popularity.desc"
    elif sort == "top_rated":
        params["sort_by"] = "vote_average.desc"
        params["vote_count.gte"] = _TOP_RATED_VOTES[media_type]
    else:
        params["sort_by"] = f"{released}.desc"
        params["vote_count.gte"] = _NEWEST_VOTES
        today = now_utc().date().isoformat()
        until = min(until, today) if until else today
    if until is not None:
        params[f"{released}.lte"] = until
    return params


def _page_entry(
    client: TmdbClient, media_type: str, genre_id: int, sort: str, decade: str | None, page: int
) -> tuple[str, Callable[[], dict[str, Any]]]:
    """The cache key and the fetch of one page of one kind's list."""

    def fetch() -> dict[str, Any]:
        try:
            payload = client.discover(
                media_type, _discover_params(media_type, genre_id, sort, decade, page)
            )
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            raise _failure(exc, "explore") from exc
        pages, total = payload.get("total_pages"), payload.get("total_results")
        return {
            "items": [
                {**{f: r[f] for f in _CARD_FIELDS if f in r}, "media_type": media_type}
                for r in dicts(payload["results"])
                if not r.get("adult")
            ],
            "pages": pages if isinstance(pages, int) else MAX_PAGE,
            "total": total if isinstance(total, int) else 0,
        }

    return f"tmdb:explore:v1:{media_type}:{genre_id}:{sort}:{decade or 'any'}:{page}", fetch


def explore_page(
    client: TmdbClient,
    genre: Genre,
    *,
    media_type: str = "all",
    sort: str = "popular",
    decade: str | None = None,
    page: int = 1,
    after: str | None = None,
    allow_fetch: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int, int | str | None]:
    """The TMDB items of one page of *genre*, how many titles the list has, and the page
    after it or None at its end. "all" is films and TV together, asked for apart, and
    *allow_fetch* (as in ``search``) is asked for each page of TMDB's. With *after* ("" to
    begin with) a list of both that has an order keeps it from page to page: what follows is
    the *after* of the next page: of each kind, the page of TMDB's and the titles last shown
    ("3-871-12,2-55"). Raises TmdbUnavailable, TmdbBusy when TMDB answered 429."""
    kinds = {
        kind: genre_id
        for kind in (genre.types if media_type == "all" else [media_type])
        if (genre_id := genre.tmdb_id(kind)) is not None
    }

    def read(kind: str, number: int) -> dict[str, Any]:
        key, fetch = _page_entry(client, kind, kinds[kind], sort, decade, number)
        page: dict[str, Any] = _cached(client, key, 6 * _HOUR, 7 * _DAY, fetch, allow_fetch)
        return page

    if after is not None and len(kinds) > 1 and sort != "popular":
        return _merged(read, list(kinds), sort, after)
    pages = [read(kind, page) for kind in kinds]
    more = any(page < min(p["pages"], MAX_PAGE) for p in pages)
    items = _mixed([p["items"] for p in pages], sort)
    return items, sum(p["total"] for p in pages), page + 1 if more else None


def _mixed(lists: Sequence[list[dict[str, Any]]], sort: str) -> list[dict[str, Any]]:
    """Films and TV in one list. TMDB's popularity of a film and of a show do not compare,
    so those two take turns."""
    turns = [
        items[place]
        for place in range(max(map(len, lists), default=0))
        for items in lists
        if place < len(items)
    ]
    if sort == "popular" or len(lists) < 2:
        return turns
    if sort == "top_rated":
        return sorted(turns, key=lambda i: -(vote_of(i.get("vote_average")) or 0))
    return sorted(turns, key=_released, reverse=True)


class _Walk:
    """One kind's list from where a page ended: TMDB's page *page*, without the titles
    *shown*. TMDB may order it anew between two reads, so it is known by those, not by how
    far down it was."""

    def __init__(
        self,
        read: Callable[[int], dict[str, Any]],
        rank: Callable[[Mapping[str, Any]], tuple[float, str]],
    ) -> None:
        self._read = read
        self._rank = rank
        self.page = 1
        self.shown: list[int] = []
        self.total = 0
        self.ended = False
        self._end = MAX_PAGE
        self._held: list[dict[str, Any]] = []

    def needs_page(self) -> bool:
        return not self.ended and not self._held

    def next_page(self) -> None:
        got = self._read(self.page)
        self.total = got["total"]
        self._end = min(got["pages"], MAX_PAGE)
        rows = [i for i in got["items"] if tmdb_id_of(i) not in (None, *self.shown)]
        self._held = sorted(rows, key=self._rank, reverse=True)
        self._move_on()

    def head(self) -> dict[str, Any] | None:
        return self._held[0] if self._held else None

    def take(self) -> dict[str, Any]:
        item = self._held.pop(0)
        self.shown.append(int(item["id"]))
        self._move_on()
        return item

    def place(self) -> str:
        return "-".join(map(str, [self.page, *self.shown[-_PLACE_MAX:]]))

    def _move_on(self) -> None:
        if self._held:
            return
        if self.page < self._end:
            self.page += 1
        else:
            self.ended = True


def _merged(
    read: Callable[[str, int], dict[str, Any]], kinds: Sequence[str], sort: str, after: str
) -> tuple[list[dict[str, Any]], int, str | None]:
    """The page of *kinds* in one order that comes after *after*, how many titles there are,
    and the next *after*. A page reads no more of TMDB's pages the further on it is, and one
    that cannot be had now ends it early."""

    def rank(item: Mapping[str, Any]) -> tuple[float, str]:
        if sort == "top_rated":
            return vote_of(item.get("vote_average")) or 0, ""
        return 0, _released(item)

    walks = [_Walk(functools.partial(read, kind), rank) for kind in kinds]
    if after:
        for walk, place in zip(walks, after.split(","), strict=True):
            walk.page, *walk.shown = map(int, place.split("-"))
    items: list[dict[str, Any]] = []
    reads = 0
    while len(items) < _MERGED_PAGE:
        need = next((walk for walk in walks if walk.needs_page()), None)
        if need is not None:
            if reads == _MERGED_READS:
                break
            reads += 1
            try:
                need.next_page()
            except (TmdbUnavailable, disk_cache.Refused):
                if not items:
                    raise
                break
            continue
        heads = [(head, walk) for walk in walks if (head := walk.head()) is not None]
        if not heads:
            break
        items.append(max(heads, key=lambda h: rank(h[0]))[1].take())
    more = not all(walk.ended for walk in walks)
    next_after = ",".join(walk.place() for walk in walks) if more else None
    return items, sum(walk.total for walk in walks), next_after


def _votes(value: object) -> int:
    return value if isinstance(value, int) and 0 <= value < 10**12 else 0


def _released(item: Mapping[str, Any]) -> str:
    return str(item.get("release_date") or item.get("first_air_date") or "")


def genre_posters(client: TmdbClient) -> dict[str, list[str]]:
    """Up to two posters of each genre, from popular titles already cached and none on two
    genres. Lists not cached are fetched in the background for the next call, until one
    fails: the rest would wait a thread out in turn."""
    used: set[str] = set()
    found: dict[str, list[str]] = {}
    failed = threading.Event()
    for genre in GENRES.values():
        media_type = genre.types[0]
        genre_id = genre.tmdb_id(media_type)
        assert genre_id is not None
        key, fetch = _page_entry(client, media_type, genre_id, "popular", None, 1)
        page = _held_page(client, key, fetch, failed)
        urls = [tmdb_image_url(i.get("poster_path"), "w185") for i in page["items"]] if page else []
        picked = [u for u in dict.fromkeys(urls) if u and u not in used][:_TILE_POSTERS]
        used.update(picked)
        found[genre.id] = picked
    return found


def _held_page(
    client: TmdbClient, key: str, fetch: Callable[[], Any], failed: threading.Event
) -> dict[str, Any] | None:
    def fetch_until_one_fails() -> Any:
        try:
            if failed.is_set():
                raise TmdbUnavailable("not asked")
            gate.check(client.token)
        except TmdbUnavailable:
            _tiles_asked.pop(key, None)
            raise
        try:
            return _noted(client, fetch)
        except TmdbUnavailable:
            failed.set()
            raise

    now = _clock()
    page: dict[str, Any] | None
    if now - _tiles_asked.get(key, -_TILES_PAUSE_S) >= _TILES_PAUSE_S:
        _tiles_asked[key] = now
        page = _discover._cache().peek(key, 6 * _HOUR, 7 * _DAY, fetch_until_one_fails, brief=True)
        return page
    try:
        page = _discover._cache().get_or_fetch(
            key, 7 * _DAY, 7 * _DAY, _not_asked, allow_fetch=lambda: False
        )
    except (TmdbUnavailable, disk_cache.Refused):
        return None
    return page


def _not_asked() -> Any:
    raise TmdbUnavailable("not asked")


def people(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The people among a search's items, as ``search`` gives them, best match first."""
    listed = []
    for item in found:
        person_id, name = tmdb_id_of(item), item.get("name")
        if item.get("media_type") != "person" or person_id is None or not name:
            continue
        titles = [
            t.get("title") or t.get("name")
            for t in dicts(item.get("known_for"))
            if not t.get("adult")
        ]
        listed.append(
            {
                "person_id": person_id,
                "name": str(name),
                "job": _job(item.get("known_for_department")),
                "profile_url": tmdb_image_url(item.get("profile_path"), "w185"),
                "known_for": [t for t in titles if isinstance(t, str) and t],
            }
        )
    return listed[:PEOPLE_LIMIT]


def _job(department: object) -> str | None:
    return _JOBS.get(department) if isinstance(department, str) else None


_wall = time.time


def person(
    client: TmdbClient, person_id: int, *, allow_fetch: Callable[[], bool] | None = None
) -> dict[str, Any]:
    """A person's facts, and their ``credits`` with the best known first: the fields of a
    card, ``role``, and ``minor`` for a title they only appear in. Raises PersonNotFound,
    TmdbUnavailable and TmdbBusy; *allow_fetch* is asked as in ``search``."""

    def fetch() -> dict[str, Any]:
        try:
            payload = client.person(person_id)
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            if _is_gone(exc):
                return {"gone": True, "at": _wall()}
            raise _failure(exc, "person") from exc
        # Stored, so that a person held before TMDB marked or dropped them is not served from
        # then on.
        if payload.get("adult"):
            return {"adult": True}
        return _person_fields(payload)

    # Only these fields are cached: TMDB's answer carries the plot of every title.
    result: dict[str, Any] = _cached(
        client, f"tmdb:person:v1:{person_id}", 7 * _DAY, 90 * _DAY, fetch, allow_fetch
    )
    if result.get("gone") and _wall() - float(result.get("at", 0)) >= _DAY:
        # A page TMDB had none for may come later: only the answer of a day ago is asked again.
        result = _cached(client, f"tmdb:person:v1:{person_id}", 0, 0, fetch, allow_fetch)
    if result.get("adult") or result.get("gone"):
        raise PersonNotFound("person")
    return result


def _person_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    department = payload.get("known_for_department")
    return {
        "name": str(payload["name"]),
        "job": _job(department),
        "profile_url": tmdb_image_url(payload.get("profile_path"), "w185"),
        "photo_url": tmdb_image_url(payload.get("profile_path"), "w342"),
        "birthday": _iso_date(payload.get("birthday")),
        "deathday": _iso_date(payload.get("deathday")),
        "biography": _shortened(str(payload.get("biography") or "").strip()),
        "credits": _credits(payload.get("combined_credits"), department),
    }


def _iso_date(value: object) -> str | None:
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


def _shortened(text: str) -> str:
    if len(text) <= BIOGRAPHY_MAX:
        return text
    cut = text[:BIOGRAPHY_MAX]
    ends = [m.end() for m in re.finditer(r"[.!?][\"”')]?(?=\s)", cut)]
    # A first sentence that ends early ("Born in St. ...") would leave next to nothing.
    if ends and ends[-1] >= BIOGRAPHY_MAX // 3:
        return cut[: ends[-1]]
    return cut[: BIOGRAPHY_MAX - 1].rsplit(None, 1)[0].rstrip(",;:- ") + "…"


def age(birthday: str | None, deathday: str | None) -> int | None:
    """In whole years, today or at death."""
    if birthday is None:
        return None
    born = date.fromisoformat(birthday)
    until = date.fromisoformat(deathday) if deathday else now_utc().date()
    years = until.year - born.year - ((until.month, until.day) < (born.month, born.day))
    return years if years >= 0 else None


def _credits(combined: object, department: object) -> list[dict[str, Any]]:
    """Each title once, with all the parts and jobs they had in it."""
    credits = combined if isinstance(combined, Mapping) else {}
    titles: dict[tuple[str, int], dict[str, Any]] = {}
    ranks: dict[tuple[str, int], tuple[bool, int]] = {}
    rows: list[tuple[Mapping[str, Any], object, object]] = [
        (c, "Acting", c.get("character")) for c in dicts(credits.get("cast"))
    ]
    rows += [(c, c.get("department"), c.get("job")) for c in dicts(credits.get("crew"))]
    for row, row_department, part in rows:
        kind, tmdb_id = tmdb_kind(row), tmdb_id_of(row)
        if kind is None or tmdb_id is None or row.get("adult"):
            continue
        role = part.strip() if isinstance(part, str) else ""
        minor = row_department != department or (
            row_department == "Acting" and _appears_only(row, kind, role)
        )
        entry = titles.get((kind, tmdb_id))
        if entry is None:
            fields = {f: row[f] for f in _CARD_FIELDS if f in row}
            entry = titles[kind, tmdb_id] = {**fields, "roles": [], "minor": True}
        if role and role not in entry["roles"]:
            entry["roles"].append(role)
        entry["minor"] = entry["minor"] and minor
        ranks[kind, tmdb_id] = (entry["minor"], -_votes(row.get("vote_count")))
    ranked = sorted(titles, key=lambda title: ranks[title])[:_CREDITS_MAX]
    return [
        {
            **{f: v for f, v in titles[t].items() if f != "roles"},
            "role": ", ".join(titles[t]["roles"])[:_ROLE_MAX] or None,
        }
        for t in ranked
    ]


def _appears_only(row: Mapping[str, Any], kind: str, role: str) -> bool:
    if _AS_THEMSELVES.match(role):
        return True
    if kind != "tv":
        return False
    genres = row.get("genre_ids")
    if isinstance(genres, list) and any(
        isinstance(g, int) and g in _AS_THEMSELVES_GENRES for g in genres
    ):
        return True
    episodes = row.get("episode_count")
    return isinstance(episodes, int) and episodes < 3
