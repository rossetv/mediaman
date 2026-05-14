"""Detail endpoint for the search experience.

Hosts ``GET /api/search/detail/{media_type}/{tmdb_id}`` which returns the
expanded "card" payload (cast, trailer, ratings, Sonarr season state, etc.)
shown when a result is opened from the search or discover shelves.
"""

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
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.responses import respond_err

from ._enrichment import _BACKDROP_BASE, _PROFILE_BASE, _poster_url

logger = logging.getLogger(__name__)

router = APIRouter()


def _pick_trailer(videos: list[dict[str, object]]) -> str | None:
    fallback: str | None = None
    for v in videos:
        if v.get("site") != "YouTube":
            continue
        raw_key = v.get("key")
        if not raw_key or not isinstance(raw_key, str):
            continue
        if v.get("type") == "Trailer":
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
    lookup = client.lookup_series_by_tmdb(tmdb_id)
    if not lookup:
        return {"tracked": False, "seasons_in_library": set()}
    tvdb_id = lookup.get("tvdbId")
    if not tvdb_id:
        return {"tracked": False, "seasons_in_library": set()}
    all_series = list(sonarr_cache.get("sonarr_series", {}).values())
    added = next((s for s in all_series if s.get("tvdbId") == tvdb_id), None)
    if not added:
        return {"tracked": False, "seasons_in_library": set()}
    in_library: set[int] = {
        season_num
        for s in added.get("seasons", [])
        if (s.get("statistics") or {}).get("episodeFileCount", 0) > 0
        and (season_num := s.get("seasonNumber")) is not None
    }
    return {"tracked": True, "seasons_in_library": in_library}


def _extract_credits(
    data: dict[str, Any], media_type: str
) -> tuple[int | None, str | None, list[dict[str, object]]]:
    if media_type == "movie":
        runtime = data.get("runtime")
        director: str | None = next(
            (
                c.get("name")
                for c in (data.get("credits") or {}).get("crew", [])
                if c.get("job") == "Director"
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
            "name": c.get("name"),
            "character": c.get("character"),
            "profile_url": f"{_PROFILE_BASE}{c['profile_path']}" if c.get("profile_path") else None,
        }
        for c in cast_raw[:6]
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
        except (_requests.RequestException, SafeHTTPError, sqlite3.Error):
            logger.warning("Radarr cache build failed during detail fetch", exc_info=True)
            radarr_cache = build_radarr_cache(None)
        return radarr_cache, build_sonarr_cache(None), None

    radarr_cache = build_radarr_cache(None)
    try:
        sonarr_client = build_sonarr_from_db(conn, secret_key)
        sonarr_cache = build_sonarr_cache(sonarr_client)
    except (_requests.RequestException, SafeHTTPError, sqlite3.Error):
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
    """Assemble the flat detail-card output dict from already-fetched data.

    Pure transform: shapes the TMDB payload, the OMDb *ratings*, and the
    computed download *state* into the JSON envelope the search/discover
    UI consumes. OMDb rating fields are only added when present so the
    client can distinguish "not rated" from "rated zero".
    """
    title = data.get("title") or data.get("name") or ""
    date = data.get("release_date") or data.get("first_air_date") or ""
    year = int(date[:4]) if date[:4].isdigit() else None

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
        "backdrop_url": f"{_BACKDROP_BASE}{data['backdrop_path']}"
        if data.get("backdrop_path")
        else None,
        "runtime": runtime,
        "genres": [g["name"] for g in data.get("genres", [])],
        "director": director,
        "cast": cast,
        "trailer_key": trailer_key,
        "rating_tmdb": round(data["vote_average"], 1) if data.get("vote_average") else None,
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
    """Build the TV-only ``(sonarr_tracked, seasons)`` pair.

    Season zero (specials) is filtered out; each season is flagged
    ``in_library`` against the Sonarr-tracked season set.
    """
    seasons_in_lib = sonarr_info["seasons_in_library"]
    seasons = [
        {
            "season_number": s["season_number"],
            "name": s.get("name") or f"Season {s['season_number']}",
            "episode_count": s.get("episode_count", 0),
            "year": int(s["air_date"][:4])
            if s.get("air_date") and s["air_date"][:4].isdigit()
            else None,
            "in_library": s["season_number"] in seasons_in_lib,
        }
        for s in data.get("seasons", [])
        if s.get("season_number", 0) > 0
    ]
    return sonarr_info["tracked"], seasons


@router.get("/api/search/detail/{media_type}/{tmdb_id}")
def api_detail(
    media_type: str,
    tmdb_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return expanded detail card for a single TMDB movie or TV series.

    Fetches full TMDB metadata (credits, videos, season summaries), builds
    Radarr/Sonarr caches to annotate download state, and fetches OMDb ratings.
    Used by the search/discover UI when a result card is opened.

    Args:
        media_type: Either ``"movie"`` or ``"tv"``; 400 on any other value.
        tmdb_id: TMDB numeric identifier for the item.
        request: Incoming FastAPI request (provides app state and secret key).
        admin: Authenticated admin username.

    Returns:
        JSON detail payload, or an error response on misconfiguration or
        TMDB fetch failure.
    """
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")

    conn = get_db()
    secret_key = request.app.state.config.secret_key
    client = TmdbClient.from_db(conn, secret_key)
    if client is None:
        return respond_err("tmdb_not_configured", status=502)

    raw_data = client.details(media_type, tmdb_id)
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

    date = data.get("release_date") or data.get("first_air_date") or ""
    year = int(date[:4]) if date[:4].isdigit() else None
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
