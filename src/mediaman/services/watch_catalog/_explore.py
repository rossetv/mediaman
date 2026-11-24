"""TMDB titles by genre, and people with everything they worked on, for Watch. Cached as
``_discover`` caches: TMDB's own answers only, and of those only what is shown. Nothing
TMDB marks adult is kept."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

import requests

from mediaman.core.time import now_utc
from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.tmdb import TmdbClient

from . import _discover
from ._discover import _CARD_FIELDS, _DAY, _HOUR, MAX_PAGE, TmdbUnavailable, _cached, _failure
from ._genres import GENRES, Genre
from ._shape import tmdb_id_of, tmdb_image_url, tmdb_kind
from ._title import _dicts

TYPES = ("all", "movie", "tv")
SORTS = ("popular", "top_rated", "newest")
_FIRST_DECADE = 1930

# Below these an average is a handful of people's.
_TOP_RATED_VOTES = {"movie": 300, "tv": 200}
_NEWEST_VOTES = 10

_TILE_POSTERS = 2

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
                for r in _dicts(payload["results"])
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
    allow_fetch: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int, int | None]:
    """The TMDB items of one page of *genre*, how many titles the list has, and the page
    after it or None at its end. "all" is a page of films and a page of TV together, asked
    for apart, and *allow_fetch* (as in ``search``) is asked for each. Raises
    TmdbUnavailable, TmdbBusy when TMDB answered 429."""
    pages = []
    for kind in genre.types if media_type == "all" else [media_type]:
        genre_id = genre.tmdb_id(kind)
        if genre_id is None:
            continue
        key, fetch = _page_entry(client, kind, genre_id, sort, decade, page)
        pages.append(_cached(key, 6 * _HOUR, 7 * _DAY, fetch, allow_fetch))
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
        return sorted(turns, key=lambda i: -_number(i.get("vote_average")))
    return sorted(turns, key=_released, reverse=True)


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _released(item: Mapping[str, Any]) -> str:
    return str(item.get("release_date") or item.get("first_air_date") or "")


def genre_posters(client: TmdbClient) -> dict[str, list[str]]:
    """Up to two posters of each genre, from popular titles already cached and none on two
    genres. A list not cached is fetched in the background for the next call."""
    used: set[str] = set()
    found: dict[str, list[str]] = {}
    for genre in GENRES.values():
        media_type = genre.types[0]
        genre_id = genre.tmdb_id(media_type)
        assert genre_id is not None
        key, fetch = _page_entry(client, media_type, genre_id, "popular", None, 1)
        page = _discover._cache().peek(key, 6 * _HOUR, 7 * _DAY, fetch)
        urls = [tmdb_image_url(i.get("poster_path"), "w185") for i in page["items"]] if page else []
        picked = [u for u in dict.fromkeys(urls) if u and u not in used][:_TILE_POSTERS]
        used.update(picked)
        found[genre.id] = picked
    return found


def people(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The people among a search's items, as ``search`` gives them, best match first."""
    listed = []
    for item in found:
        person_id, name = tmdb_id_of(item), item.get("name")
        if item.get("media_type") != "person" or person_id is None or not name:
            continue
        titles = [
            t.get("title") or t.get("name")
            for t in _dicts(item.get("known_for"))
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


def person(
    client: TmdbClient, person_id: int, *, allow_fetch: Callable[[], bool] | None = None
) -> dict[str, Any]:
    """A person's facts, and their ``credits`` with the best known first: the fields of a
    card, ``role``, and ``minor`` for a title they only appear in. Raises PersonNotFound,
    TmdbUnavailable and TmdbBusy; *allow_fetch* is asked as in ``search``."""

    def fetch() -> dict[str, Any]:
        try:
            payload = client.person(person_id)
        except SafeHTTPError as exc:
            if exc.status_code == 404:
                raise PersonNotFound("person") from exc
            raise _failure(exc, "person") from exc
        except (requests.RequestException, ValueError) as exc:
            raise _failure(exc, "person") from exc
        if payload.get("adult"):
            raise PersonNotFound("person")
        return _person_fields(payload)

    # Only these fields are cached: TMDB's answer carries the plot of every title.
    result: dict[str, Any] = _cached(
        f"tmdb:person:v1:{person_id}", 7 * _DAY, 90 * _DAY, fetch, allow_fetch
    )
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
    ranks: dict[tuple[str, int], tuple[bool, float]] = {}
    rows: list[tuple[Mapping[str, Any], object, object]] = [
        (c, "Acting", c.get("character")) for c in _dicts(credits.get("cast"))
    ]
    rows += [(c, c.get("department"), c.get("job")) for c in _dicts(credits.get("crew"))]
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
        ranks[kind, tmdb_id] = (entry["minor"], -_number(row.get("vote_count")))
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
    if isinstance(genres, list) and any(g in _AS_THEMSELVES_GENRES for g in genres):
        return True
    episodes = row.get("episode_count")
    return isinstance(episodes, int) and episodes < 3
