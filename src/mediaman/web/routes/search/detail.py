from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any, TypedDict
from typing import cast as _cast

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

import requests as _requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from mediaman.db import get_db
from mediaman.services import watch_catalog
from mediaman.services.arr import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import (
    ArrCaches,
    RadarrCaches,
    SonarrCaches,
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
)
from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.omdb import fetch_ratings
from mediaman.services.media_meta.tmdb import TmdbClient, vote_of, year_of
from mediaman.services.watch_catalog import tmdb_image_url
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.responses import respond_err

from ._enrichment import (
    _QUERY_LIMITER,
    _TMDB_TIMEOUT_S,
    _poster_url,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _pick_trailer(videos: list[dict[str, object]]) -> str | None:
    fallback: str | None = None
    for video in videos:
        if video.get("site") != "YouTube":
            continue
        raw_key = video.get("key")
        if not raw_key or not isinstance(raw_key, str):
            continue
        if video.get("type") == "Trailer":
            return raw_key
        if fallback is None:
            fallback = raw_key
    return fallback


class _SonarrDetail(TypedDict):
    tracked: bool
    seasons_in_library: set[int]


def _fetch_sonarr_series_detail(
    tmdb_id: int, sonarr_cache: SonarrCaches, client: ArrClient | None
) -> _SonarrDetail:
    if not client:
        return {"tracked": False, "seasons_in_library": set()}
    held = sonarr_cache.get("sonarr_series", {})
    try:
        lookup = client.lookup_series_by_tmdb(tmdb_id)
    except (_requests.RequestException, SafeHTTPError, ArrError):
        logger.warning("Sonarr lookup failed during detail fetch", exc_info=True)
        added = held.get(tmdb_id)
    else:
        tvdb_id = lookup.get("tvdbId") if lookup else None
        if not tvdb_id:
            return {"tracked": False, "seasons_in_library": set()}
        added = next((series for series in held.values() if series.get("tvdbId") == tvdb_id), None)
    if not added:
        return {"tracked": False, "seasons_in_library": set()}
    in_library: set[int] = {
        season_num
        for season in added.get("seasons", [])
        if (season.get("statistics") or {}).get("episodeFileCount", 0) > 0
        and (season_num := season.get("seasonNumber")) is not None
    }
    return {"tracked": True, "seasons_in_library": in_library}


def _extract_credits(
    data: dict[str, Any], media_type: str
) -> tuple[int | None, str | None, list[dict[str, object]]]:
    if media_type == "movie":
        runtime = data.get("runtime")
        director: str | None = next(
            (
                crew_member.get("name")
                for crew_member in (data.get("credits") or {}).get("crew", [])
                if crew_member.get("job") == "Director"
            ),
            None,
        )
    else:
        ert = data.get("episode_run_time") or []
        runtime = ert[0] if ert else None
        creators = data.get("created_by") or []
        director = creators[0].get("name") if creators else None

    cast_raw = (data.get("credits") or {}).get("cast") or []
    cast = [
        {
            "name": cast_member.get("name"),
            "character": cast_member.get("character"),
            "profile_url": tmdb_image_url(cast_member.get("profile_path"), "w185"),
        }
        for cast_member in cast_raw[:6]
    ]
    return runtime, director, cast


def _build_arr_caches(
    conn: sqlite3.Connection,
    secret_key: str,
    media_type: str,
) -> tuple[RadarrCaches, SonarrCaches, ArrClient | None]:
    if media_type == "movie":
        try:
            radarr_cache = build_radarr_cache(build_radarr_from_db(conn, secret_key))
        except (_requests.RequestException, SafeHTTPError, ArrError, sqlite3.Error):
            logger.warning("Radarr cache build failed during detail fetch", exc_info=True)
            radarr_cache = build_radarr_cache(None)
        return radarr_cache, build_sonarr_cache(None), None

    radarr_cache = build_radarr_cache(None)
    try:
        sonarr_client = build_sonarr_from_db(conn, secret_key)
        sonarr_cache = build_sonarr_cache(sonarr_client)
    except (_requests.RequestException, SafeHTTPError, ArrError, sqlite3.Error):
        logger.warning("Sonarr cache build failed during detail fetch", exc_info=True)
        sonarr_client = None
        sonarr_cache = build_sonarr_cache(None)
    return radarr_cache, sonarr_cache, sonarr_client


def _build_detail_payload(
    data: dict[str, Any],
    media_type: str,
    tmdb_id: int,
    ratings: dict[str, str],
    state: object,
) -> dict[str, object]:
    """Assemble the flat detail-card dict from already-fetched data. OMDb rating fields are
    added only when present, so the client can tell "not rated" from "rated zero".
    """
    title = data.get("title") or data.get("name") or ""
    year = year_of(data.get("release_date") or data.get("first_air_date"))

    runtime, director, cast = _extract_credits(data, media_type)
    trailer_key = _pick_trailer((data.get("videos") or {}).get("results") or [])

    out: dict[str, object] = {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "tagline": data.get("tagline") or None,
        "description": data.get("overview") or "",
        "poster_url": _poster_url(data.get("poster_path")),
        "backdrop_url": tmdb_image_url(data.get("backdrop_path"), "w780"),
        "runtime": runtime,
        "genres": [g["name"] for g in data.get("genres", [])],
        "director": director,
        "cast": cast,
        "trailer_key": trailer_key,
        "rating_tmdb": vote_of(data.get("vote_average")) or None,
        "download_state": state,
    }
    if "imdb" in ratings:
        out["rating_imdb"] = ratings["imdb"]
    if "rt" in ratings:
        out["rating_rt"] = ratings["rt"]
    if "metascore" in ratings:
        out["rating_metascore"] = ratings["metascore"]
    return out


def _build_seasons_block(
    data: dict[str, Any], sonarr_info: _SonarrDetail
) -> tuple[bool, list[dict[str, object]]]:
    """Build the TV-only (sonarr_tracked, seasons) pair, dropping season zero (specials)."""
    seasons_in_lib = sonarr_info["seasons_in_library"]
    seasons = [
        {
            "season_number": season["season_number"],
            "name": season.get("name") or f"Season {season['season_number']}",
            "episode_count": season.get("episode_count", 0),
            "year": year_of(season.get("air_date")),
            "in_library": season["season_number"] in seasons_in_lib,
        }
        for season in data.get("seasons", [])
        if season.get("season_number", 0) > 0
    ]
    return sonarr_info["tracked"], seasons


@router.get("/api/search/detail/{media_type}/{tmdb_id}")
def api_detail(
    media_type: str,
    tmdb_id: int,
    request: Request,
    admin: str = Depends(get_current_user),
) -> JSONResponse:
    """Return the expanded detail card for one TMDB movie or TV series; 400 for any media_type
    other than "movie" or "tv".
    """
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")
    if not _QUERY_LIMITER.check(admin):
        return respond_err(
            "too_many_requests", status=429, message="Too many search requests — slow down"
        )

    conn = get_db()
    secret_key = request.app.state.config.secret_key
    client = TmdbClient.from_db(conn, secret_key, timeout=_TMDB_TIMEOUT_S, retry=False)
    if client is None:
        return respond_err("tmdb_not_configured", status=502)

    try:
        with watch_catalog.gate.asking(client.token):
            raw_data = client.details(media_type, tmdb_id)
    except watch_catalog.TmdbBusy:
        return respond_err("too_many_requests", status=429)
    except watch_catalog.TmdbUnavailable:
        raw_data = None
    if raw_data is None:
        return respond_err("tmdb_request_failed", status=502)
    # rationale: ``TmdbClient.details`` returns ``dict[str, object]`` to
    # describe the TMDB payload generically; the handler and its payload
    # helpers read dozens of well-known TMDB fields (title, release_date,
    # runtime, credits, videos, seasons, ...).  A TypedDict for the full
    # TMDB detail response would mirror the dozens of fields TMDB ships
    # across movies and TV — far more than the ones mediaman reads.  Each
    # access is guarded by ``.get()`` and isinstance/type checks before use.
    data = _cast(dict[str, Any], raw_data)

    if data.get("adult"):
        return respond_err("not_found", status=404)
    year = year_of(data.get("release_date") or data.get("first_air_date"))
    title = data.get("title") or data.get("name") or ""
    ratings = fetch_ratings(title, year, media_type, conn=conn, secret_key=secret_key)

    radarr_cache, sonarr_cache, sonarr_client = _build_arr_caches(conn, secret_key, media_type)
    caches: ArrCaches = {**radarr_cache, **sonarr_cache}
    state = compute_download_state(media_type, tmdb_id, caches)

    out = _build_detail_payload(data, media_type, tmdb_id, ratings, state)

    if media_type == "tv":
        sonarr_info = _fetch_sonarr_series_detail(tmdb_id, sonarr_cache, sonarr_client)
        out["sonarr_tracked"], out["seasons"] = _build_seasons_block(data, sonarr_info)

    return JSONResponse(out)
