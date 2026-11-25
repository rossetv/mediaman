"""TMDB response-shape types and pure, network-free shaping helpers.

TmdbClient re-binds shape_card and shape_detail as static methods, so
TmdbClient.shape_card(...) works.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

_YEAR_RE = re.compile(r"[0-9]{4}")

_POSTER_BASE_W300 = "https://image.tmdb.org/t/p/w300"


def vote_of(value: object) -> float | None:
    """A TMDB vote average to one decimal; None for anything that is none."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 10:
        return None
    return round(float(value), 1)


def year_of(date: object) -> int | None:
    """The year of a TMDB date ("2019-03-01")."""
    found = _YEAR_RE.match(date) if isinstance(date, str) else None
    return int(found.group()) if found else None


def dicts(value: object) -> list[Mapping[str, Any]]:
    """The objects of a JSON list; none when *value* is no list."""
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


def dicts_in(value: object, name: str) -> list[Mapping[str, Any]]:
    """The objects of the list *name* of a JSON object; none when there is no such list."""
    return dicts(value.get(name)) if isinstance(value, Mapping) else []


def _minutes(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class TmdbSearchResult(TypedDict, total=False):
    """Subset of a TMDB ``/search/{movie|tv|multi}`` result item.

    ``total=False`` because TMDB habitually omits optional keys from results.
    Only the fields mediaman actually reads are listed; the upstream payload
    includes many more.
    """

    id: int
    title: str
    name: str  # TV shows use ``name`` instead of ``title``
    release_date: str
    first_air_date: str  # TV shows
    poster_path: str | None
    vote_average: float
    overview: str
    media_type: str  # present in /search/multi results


class TmdbDetailsPayload(TypedDict, total=False):
    """Subset of a TMDB ``/{movie|tv}/{id}`` details response with appended sub-resources.

    ``total=False`` because optional fields are omitted when empty.
    The ``videos`` and ``credits`` sub-dicts are appended via
    ``?append_to_response=videos,credits``.
    """

    id: int
    title: str
    name: str
    release_date: str
    first_air_date: str
    poster_path: str | None
    vote_average: float
    overview: str
    tagline: str
    runtime: int
    episode_run_time: list[int]
    genres: list[dict[str, object]]
    created_by: list[dict[str, object]]
    credits: dict[str, object]
    videos: dict[str, object]
    release_dates: dict[str, object]  # movies
    content_ratings: dict[str, object]  # TV


class TmdbCard(TypedDict):
    """Compact card shape (w300 poster) for search results and recommendation tiles."""

    tmdb_id: int | None
    year: int | None
    poster_url: str | None
    rating: float | None
    description: str


class TmdbDetail(TypedDict, total=False):
    """Rich detail shape merged into the card dict; all fields are optional because some
    exist only for certain media types.
    """

    tagline: str | None
    description: str
    runtime: int | None
    genres: str | None  # JSON-encoded list of genre name strings, or None
    cast_json: str | None  # JSON-encoded list of name, character and, with a photo, profile_path
    director: str | None
    trailer_key: str | None


# rationale: ``data`` is a TMDB ``/search/{movie,tv}`` or ``/{movie,tv}/{id}``
# response.  Both endpoints emit dozens of fields beyond what mediaman reads.
# ``Mapping[str, Any]`` is used so callers can pass either a raw dict or a
# TypedDict (``TmdbSearchResult`` / ``TmdbDetailsPayload``) without a cast;
# field access uses ``.get()`` and isinstance guards, so the ``Any``
# value-side is contained inside this method body.
def shape_card(data: Mapping[str, Any]) -> TmdbCard:
    """Return the compact-card shape for a TMDB search / details payload.

    The poster is w300 and the rating is rounded to 1dp, matching the other TMDB card
    paths.
    """
    poster_path = data.get("poster_path")
    poster_url = (
        f"{_POSTER_BASE_W300}{poster_path}"
        if isinstance(poster_path, str) and poster_path.startswith("/")
        else None
    )

    return {
        "tmdb_id": data.get("id"),
        "year": year_of(data.get("release_date") or data.get("first_air_date")),
        "poster_url": poster_url,
        "rating": vote_of(data.get("vote_average")),
        "description": data.get("overview") or "",
    }


# rationale: same reason as shape_card — TMDB's ``/{movie,tv}/{id}``
# detail response is wide and frequently extended.  ``Mapping[str, Any]``
# accepts both raw dicts and ``TmdbDetailsPayload`` TypedDicts; field access
# is guarded by ``.get()`` plus isinstance checks within the body.
def shape_detail(data: Mapping[str, Any], *, media_type: str) -> TmdbDetail:
    """Return the rich-detail shape for a TMDB details payload.

    genres and cast_json are JSON strings matching the suggestions table columns, so
    callers merge them directly. director is the director for movies and the first
    creator for TV.
    """
    out: TmdbDetail = {
        "tagline": data.get("tagline") or None,
        "description": data.get("overview") or "",
    }

    endpoint = "movie" if media_type == "movie" else "tv"
    if endpoint == "movie":
        out["runtime"] = _minutes(data.get("runtime"))
    else:
        ert = data.get("episode_run_time")
        out["runtime"] = _minutes(ert[0]) if isinstance(ert, list) and ert else None

    genres = [g["name"] for g in dicts(data.get("genres")) if isinstance(g.get("name"), str)]
    out["genres"] = json.dumps(genres) if genres else None

    director: str | None = None
    if endpoint == "movie":
        crew = dicts_in(data.get("credits"), "crew")
        director = next((c.get("name") for c in crew if c.get("job") == "Director"), None)
    else:
        creators = dicts(data.get("created_by"))
        if creators:
            director = creators[0].get("name")
    out["director"] = director

    cast = dicts_in(data.get("credits"), "cast")[:8]
    if cast:
        out["cast_json"] = json.dumps(
            [
                {"name": c.get("name"), "character": c.get("character", "")}
                | ({"profile_path": c["profile_path"]} if c.get("profile_path") else {})
                for c in cast
            ]
        )
    else:
        out["cast_json"] = None

    trailer_key: str | None = None
    for v in dicts_in(data.get("videos"), "results"):
        if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("key"):
            trailer_key = v["key"]
            break
    out["trailer_key"] = trailer_key

    return out


def certificate(details: Mapping[str, Any], kind: Literal["movie", "tv"]) -> str | None:
    """Return the US age certificate from a details payload, or None when TMDB has none.

    Needs ``release_dates`` (movie) or ``content_ratings`` (tv) appended to the request.
    """
    if kind == "movie":
        for country in dicts_in(details.get("release_dates"), "results"):
            if country.get("iso_3166_1") != "US":
                continue
            for rel in dicts(country.get("release_dates")):
                cert = rel.get("certification")
                if isinstance(cert, str) and cert.strip():
                    return cert.strip()
        return None
    for entry in dicts_in(details.get("content_ratings"), "results"):
        if entry.get("iso_3166_1") == "US":
            rating = entry.get("rating")
            return (rating.strip() or None) if isinstance(rating, str) else None
    return None
