"""The parts of a title's detail that come from TMDB."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mediaman.services.media_meta.tmdb import TmdbClient, dicts, dicts_in, year_of

from ._genres import genre_refs
from ._shape import tmdb_id_of, tmdb_image_url, tmdb_rating

CAST_LIMIT = 12


def pick_trailer(details: Mapping[str, Any]) -> str | None:
    """The YouTube key of the first trailer, else of the first YouTube video. Not checked:
    the caller must pass it through validate_youtube_id."""
    fallback: str | None = None
    for video in dicts_in(details.get("videos"), "results"):
        key = video.get("key")
        if video.get("site") != "YouTube" or not isinstance(key, str) or not key:
            continue
        if video.get("type") == "Trailer":
            return key
        fallback = fallback or key
    return fallback


def _director(details: Mapping[str, Any], kind: str) -> tuple[str | None, int | None]:
    if kind == "movie":
        crew = dicts_in(details.get("credits"), "crew")
        found = next((c for c in crew if c.get("job") == "Director"), {})
    else:
        found = next(iter(dicts(details.get("created_by"))), {})
    name = found.get("name")
    return (name, _person_id(found)) if isinstance(name, str) and name else (None, None)


def _person_id(person: Mapping[str, Any]) -> int | None:
    # TMDB marks some people adult; those have no page here to link to.
    return None if person.get("adult") else tmdb_id_of(person)


def _runtime(details: Mapping[str, Any], kind: str) -> int | None:
    if kind == "movie":
        value = details.get("runtime")
    else:
        runs = details.get("episode_run_time")
        last = details.get("last_episode_to_air")
        # TMDB leaves the first empty for most shows now; their latest episode still says.
        value = (
            runs[0]
            if isinstance(runs, list) and runs
            else last.get("runtime")
            if isinstance(last, Mapping)
            else None
        )
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _number(value: object) -> float:
    valid = isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value < 10**12
    return float(value) if valid else 0.0  # type: ignore[arg-type]


def tmdb_fields(details: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """Title, dates, people, pictures and seasons of a TMDB details payload ("movie" or
    "tv"). ``trailer_key`` is unchecked, ``tmdb_seasons`` is TMDB's season list,
    specials left out, ``tvdb_id`` is a show's TVDB id when TMDB knows it, and
    ``collection_id`` that of the collection a film is part of."""
    cast = dicts_in(details.get("credits"), "cast")
    director, director_id = _director(details, kind)
    released = details.get("release_date") or details.get("first_air_date")
    year = year_of(released)
    year_end = year_of(details.get("last_air_date")) if kind == "tv" else None
    seasons = [
        {
            "number": s["season_number"],
            "name": str(s.get("name") or f"Season {s['season_number']}"),
            "episode_count": s.get("episode_count")
            if isinstance(s.get("episode_count"), int)
            else None,
            "air_date": s.get("air_date") if isinstance(s.get("air_date"), str) else None,
        }
        for s in dicts(details.get("seasons"))
        if isinstance(s.get("season_number"), int) and s["season_number"] > 0
    ]
    count = details.get("number_of_seasons")
    external = details.get("external_ids")
    collection = details.get("belongs_to_collection")
    tvdb_id = external.get("tvdb_id") if isinstance(external, Mapping) else None
    return {
        "title": str(details.get("title") or details.get("name") or ""),
        "year": year,
        "release_date": released if year else None,
        "year_end": year_end if year_end != year else None,
        "runtime_min": _runtime(details, kind),
        "season_count": (count if isinstance(count, int) else len(seasons))
        if kind == "tv"
        else None,
        "genres": [
            g["name"] for g in dicts(details.get("genres")) if isinstance(g.get("name"), str)
        ],
        "genre_refs": genre_refs(details.get("genres"), kind),
        "certificate": TmdbClient.certificate(details, "movie" if kind == "movie" else "tv"),
        "tagline": str(details.get("tagline") or "") or None,
        "overview": str(details.get("overview") or ""),
        "backdrop_url": tmdb_image_url(details.get("backdrop_path"), "w1280"),
        "poster_url": tmdb_image_url(details.get("poster_path"), "w500"),
        "tmdb_rating": tmdb_rating(details.get("vote_average")),
        "popularity": _number(details.get("popularity")),
        "vote_count": int(_number(details.get("vote_count"))),
        "director": director,
        "director_id": director_id,
        "cast": [
            {
                "person_id": _person_id(c),
                "name": str(c.get("name") or ""),
                "character": str(c.get("character") or ""),
                "profile_url": tmdb_image_url(c.get("profile_path"), "w185"),
            }
            for c in cast[:CAST_LIMIT]
        ],
        "trailer_key": pick_trailer(details),
        "tmdb_seasons": seasons,
        "collection_id": tmdb_id_of(collection) if isinstance(collection, Mapping) else None,
        "tvdb_id": tvdb_id
        if isinstance(tvdb_id, int) and not isinstance(tvdb_id, bool) and tvdb_id > 0
        else None,
    }
