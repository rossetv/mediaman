"""TMDB response-shape types and pure shaping helpers.

This module owns the *data* side of the TMDB integration: the
``TypedDict`` descriptions of the raw TMDB JSON payloads and the pure
functions that transform a raw TMDB dict into a tightly-typed domain
shape. It is split out from :mod:`mediaman.services.media_meta.tmdb`
(which keeps the :class:`~mediaman.services.media_meta.tmdb.TmdbClient`
network client) so the file-size ceiling is respected and the pure,
network-free transforms are isolated for testing.

The shape helpers are pure functions over the TMDB JSON payload — no
network calls, no settings access — so they're trivially testable and
reusable by callers that have already fetched the raw data.
:class:`TmdbClient` re-binds :func:`shape_card` and :func:`shape_detail`
as static methods, so existing ``TmdbClient.shape_card(...)`` call sites
keep working unchanged.
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
    """Compact card shape returned by :meth:`TmdbClient.shape_card`.

    Suitable for search results and recommendation tiles; uses w300 poster size.
    """

    tmdb_id: int | None
    year: int | None
    poster_url: str | None
    rating: float | None
    description: str


class TmdbDetail(TypedDict, total=False):
    """Rich detail shape returned by :meth:`TmdbClient.shape_detail`.

    Merged into the card dict by :func:`~mediaman.services.media_meta.item_enrichment.apply_tmdb_detail`.
    All fields are optional because some are only available for certain media types.
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

    Fields: ``year`` (int | None), ``poster_url`` (str | None, w300),
    ``rating`` (float rounded to 1dp | None), ``description`` (str,
    may be empty), ``tmdb_id`` (int | None).

    Matches the shape produced inline by download.py and the
    recommendations enrichment path — both used w300 posters and
    rounded vote_average to 1dp.
    """
    date = data.get("release_date") or data.get("first_air_date") or ""
    year: int | None = None
    if date[:4].isdigit():
        try:
            year = int(date[:4])
        except ValueError:
            year = None

    poster_path = data.get("poster_path")
    poster_url = f"{_POSTER_BASE_W300}{poster_path}" if poster_path else None

    rating: float | None = None
    vote = data.get("vote_average")
    if vote:
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


# rationale: same reason as :func:`shape_card` — TMDB's ``/{movie,tv}/{id}``
# detail response is wide and frequently extended.  ``Mapping[str, Any]``
# accepts both raw dicts and ``TmdbDetailsPayload`` TypedDicts; field access
# is guarded by ``.get()`` plus isinstance checks within the body.
def shape_detail(data: Mapping[str, Any], *, media_type: str) -> TmdbDetail:
    """Return the rich-detail shape for a TMDB ``details`` payload.

    Fields: ``tagline``, ``runtime``, ``genres`` (JSON string or
    None), ``cast_json`` (JSON string of top-8 cast with ``name`` +
    ``character`` — None when empty), ``director`` (name string or
    None — director for movies, first creator for TV),
    ``trailer_key`` (YouTube key or None), ``description`` (overview
    string, may be empty).

    The JSON-encoded ``genres`` and ``cast_json`` shapes match the
    ``suggestions`` table columns and the ``items`` dict passed to
    the download template — callers merge them directly.
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
