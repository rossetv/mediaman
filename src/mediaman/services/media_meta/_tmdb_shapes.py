"""TMDB response-shape types and pure, network-free shaping helpers.

TmdbClient re-binds shape_card and shape_detail as static methods, so
TmdbClient.shape_card(...) works.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypedDict

_POSTER_BASE_W300 = "https://image.tmdb.org/t/p/w300"


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
    cast_json: str | None  # JSON-encoded list of ``{"name": ..., "character": ...}`` dicts
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
    date = data.get("release_date") or data.get("first_air_date") or ""
    year: int | None = None
    if date[:4].isdigit():
        try:
            year = int(date[:4])
        except ValueError:
            year = None

    poster_path = data.get("poster_path")
    poster_url = (
        f"{_POSTER_BASE_W300}{poster_path}" if poster_path and poster_path.startswith("/") else None
    )

    rating: float | None = None
    vote = data.get("vote_average")
    if vote is not None:
        try:
            rating = round(float(vote), 1)
        except (TypeError, ValueError):
            rating = None

    return {
        "tmdb_id": data.get("id"),
        "year": year,
        "poster_url": poster_url,
        "rating": rating,
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
        out["runtime"] = data.get("runtime")
    else:
        ert = data.get("episode_run_time") or []
        out["runtime"] = ert[0] if ert else None

    genres = [g["name"] for g in data.get("genres") or []]
    out["genres"] = json.dumps(genres) if genres else None

    director: str | None = None
    if endpoint == "movie":
        credits = data.get("credits") or {}
        director = next(
            (c.get("name") for c in credits.get("crew") or [] if c.get("job") == "Director"),
            None,
        )
    else:
        creators = data.get("created_by") or []
        if creators:
            director = creators[0].get("name")
    out["director"] = director

    credits = data.get("credits") or {}
    cast = (credits.get("cast") or [])[:8]
    if cast:
        out["cast_json"] = json.dumps(
            [{"name": c.get("name"), "character": c.get("character", "")} for c in cast]
        )
    else:
        out["cast_json"] = None

    trailer_key: str | None = None
    for v in (data.get("videos") or {}).get("results") or []:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("key"):
            trailer_key = v["key"]
            break
    out["trailer_key"] = trailer_key

    return out
