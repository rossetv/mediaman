"""The parts of a title's detail that come from TMDB."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mediaman.services.media_meta.tmdb import TmdbClient

from ._shape import tmdb_image_url, tmdb_rating, year_of

CAST_LIMIT = 12


def _dicts(value: object) -> list[Mapping[str, Any]]:
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


def _results(value: object) -> list[Mapping[str, Any]]:
    return _dicts(value.get("results")) if isinstance(value, Mapping) else []


def pick_trailer(details: Mapping[str, Any]) -> str | None:
    """The YouTube key of the first trailer, else of the first YouTube video. Not checked:
    the caller must pass it through validate_youtube_id."""
    fallback: str | None = None
    for video in _results(details.get("videos")):
        key = video.get("key")
        if video.get("site") != "YouTube" or not isinstance(key, str) or not key:
            continue
        if video.get("type") == "Trailer":
            return key
        fallback = fallback or key
    return fallback


def _director(details: Mapping[str, Any], kind: str) -> str | None:
    if kind == "movie":
        credits = details.get("credits")
        crew = _dicts(credits.get("crew")) if isinstance(credits, Mapping) else []
        name = next((c.get("name") for c in crew if c.get("job") == "Director"), None)
    else:
        creators = _dicts(details.get("created_by"))
        name = creators[0].get("name") if creators else None
    return name if isinstance(name, str) and name else None


def _runtime(details: Mapping[str, Any], kind: str) -> int | None:
    if kind == "movie":
        value = details.get("runtime")
    else:
        runs = details.get("episode_run_time")
        value = runs[0] if isinstance(runs, list) and runs else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def tmdb_fields(details: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """Title, dates, people, pictures and seasons of a TMDB details payload ("movie" or
    "tv"). ``trailer_key`` is unchecked and ``tmdb_seasons`` is TMDB's season list,
    specials left out."""
    credits = details.get("credits")
    cast = _dicts(credits.get("cast")) if isinstance(credits, Mapping) else []
    year = year_of(details.get("release_date") or details.get("first_air_date"))
    year_end = year_of(details.get("last_air_date")) if kind == "tv" else None
    seasons = [
        {
            "number": s["season_number"],
            "name": str(s.get("name") or f"Season {s['season_number']}"),
            "episode_count": s.get("episode_count")
            if isinstance(s.get("episode_count"), int)
            else None,
        }
        for s in _dicts(details.get("seasons"))
        if isinstance(s.get("season_number"), int) and s["season_number"] > 0
    ]
    count = details.get("number_of_seasons")
    return {
        "title": str(details.get("title") or details.get("name") or ""),
        "year": year,
        "year_end": year_end if year_end != year else None,
        "runtime_min": _runtime(details, kind),
        "season_count": (count if isinstance(count, int) else len(seasons))
        if kind == "tv"
        else None,
        "genres": [
            g["name"] for g in _dicts(details.get("genres")) if isinstance(g.get("name"), str)
        ],
        "certificate": TmdbClient.certificate(details, "movie" if kind == "movie" else "tv"),
        "tagline": details.get("tagline") or None,
        "overview": str(details.get("overview") or ""),
        "backdrop_url": tmdb_image_url(details.get("backdrop_path"), "w1280"),
        "poster_url": tmdb_image_url(details.get("poster_path"), "w500"),
        "tmdb_rating": tmdb_rating(details.get("vote_average")),
        "director": _director(details, kind),
        "cast": [
            {
                "name": str(c.get("name") or ""),
                "character": str(c.get("character") or ""),
                "profile_url": tmdb_image_url(c.get("profile_path"), "w185"),
            }
            for c in cast[:CAST_LIMIT]
        ],
        "trailer_key": pick_trailer(details),
        "tmdb_seasons": seasons,
    }
