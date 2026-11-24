from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from typing import Any, cast

import requests
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mediaman.crypto import generate_poll_token, validate_download_token
from mediaman.db import get_db
from mediaman.services.arr import ArrError
from mediaman.services.arr._types import SonarrSeries
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import (
    ACTION_DOWNLOADING,
    ACTION_IN_LIBRARY,
    ACTION_PARTIAL,
    ACTION_QUEUED,
    ArrCaches,
    RadarrCaches,
    SonarrCaches,
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
    series_has_files,
)
from mediaman.services.downloads.download_format import build_item
from mediaman.services.downloads.notifications import has_unsent_notification
from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.item_enrichment import (
    enrich_redownload_item,
    fill_backdrop,
    tidy_summary,
)
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.repository.download import (
    SuggestionEnrichment,
    fetch_suggestion_enrichment,
)
from mediaman.web.routes.download._cache_keys import _key_fingerprint
from mediaman.web.routes.download._season import deleted_season, season_on_disk, sonarr_media_id

# YouTube video IDs are exactly 11 URL-safe base64 characters.
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

logger = logging.getLogger(__name__)

router = APIRouter()

# Rate limiter for the public download GET endpoint.
_DOWNLOAD_LIMITER_GET = RateLimiter(max_attempts=30, window_seconds=60)

# Per-service Arr-state cache.
#
# Each GET /download/{token} would otherwise issue four outbound HTTP calls
# (Radarr movies + queue, Sonarr series + queue) on every render. With
# one valid public token, an attacker driving the rate limit (30 req/min)
# would multiply that into 120 outbound requests/min/IP — effectively a
# request amplifier against the operator's home Arr boxes. Cache the
# per-service snapshot for a short window so a burst of confirm-page
# loads collapses to one set of upstream calls.
#
# TTL is 30s: long enough to absorb a confirm-page burst, short enough
# that a state change (admin adds a movie elsewhere) is reflected on the
# next click without manual refresh. The cache is process-local so
# multi-worker deploys re-fetch per worker — that is acceptable since
# the limit is per-IP per-worker as well.

_ARR_CACHE_TTL_SECONDS = 30.0
# rationale: bounded to 20 entries (2 services × up to 10 deployment variants)
# to prevent unbounded growth — while the keyspace is practically two entries
# (radarr + sonarr for one deployment's secret key), multi-tenant and test
# scenarios can accumulate keys; a hard cap is the safety net.
_ARR_CACHE_MAX_ENTRIES = 20
_ARR_CACHE_LOCK = threading.Lock()
# Per-process Arr-state TTL cache; must be in-process state to survive
# across requests in the same worker without an extra DB or network round-trip.
# Two independently-typed dicts so each getter returns a precise subtype
# without a cast or type suppression.
_RADARR_CACHE: dict[str, tuple[float, RadarrCaches]] = {}
_SONARR_CACHE: dict[str, tuple[float, SonarrCaches]] = {}
# Counts drops, so a list fetched before a drop is not stored after it.
_arr_cache_drops = 0


def drop_arr_cache() -> None:
    """Forget the Radarr and Sonarr lists: whatever asks an Arr for a title calls this, so the
    emailed page does not offer again what is already on its way.
    """
    global _arr_cache_drops
    with _ARR_CACHE_LOCK:
        _arr_cache_drops += 1
        _RADARR_CACHE.clear()
        _SONARR_CACHE.clear()


def _get_radarr_cache_cached(conn: sqlite3.Connection, secret_key: str) -> RadarrCaches:
    """Return the Radarr cache dict from a process-wide TTL cache, re-checking under the write
    lock so concurrent misses do not each fetch.
    """
    fp = _key_fingerprint(secret_key)
    now = time.monotonic()
    with _ARR_CACHE_LOCK:
        hit = _RADARR_CACHE.get(fp)
        if hit and now - hit[0] < _ARR_CACHE_TTL_SECONDS:
            return hit[1]
        drops = _arr_cache_drops
    radarr_client = build_radarr_from_db(conn, secret_key)
    cache = build_radarr_cache(radarr_client)
    with _ARR_CACHE_LOCK:
        # Double-check: another thread may have populated the cache while
        # we were fetching.  Only write if the slot is still stale.
        hit = _RADARR_CACHE.get(fp)
        if drops == _arr_cache_drops and (not hit or now - hit[0] >= _ARR_CACHE_TTL_SECONDS):
            if len(_RADARR_CACHE) >= _ARR_CACHE_MAX_ENTRIES:
                _RADARR_CACHE.clear()
            _RADARR_CACHE[fp] = (now, cache)
    return cache


def _get_sonarr_cache_cached(conn: sqlite3.Connection, secret_key: str) -> SonarrCaches:
    fp = _key_fingerprint(secret_key)
    now = time.monotonic()
    with _ARR_CACHE_LOCK:
        hit = _SONARR_CACHE.get(fp)
        if hit and now - hit[0] < _ARR_CACHE_TTL_SECONDS:
            return hit[1]
        drops = _arr_cache_drops
    sonarr_client = build_sonarr_from_db(conn, secret_key)
    cache = build_sonarr_cache(sonarr_client)
    with _ARR_CACHE_LOCK:
        # Double-check: another thread may have populated the cache while
        # we were fetching.  Only write if the slot is still stale.
        hit = _SONARR_CACHE.get(fp)
        if drops == _arr_cache_drops and (not hit or now - hit[0] >= _ARR_CACHE_TTL_SECONDS):
            if len(_SONARR_CACHE) >= _ARR_CACHE_MAX_ENTRIES:
                _SONARR_CACHE.clear()
            _SONARR_CACHE[fp] = (now, cache)
    return cache


def _reset_arr_cache_for_tests() -> None:
    drop_arr_cache()


def validate_youtube_id(s: str | None) -> str | None:
    if not s:
        return None
    return s if _YOUTUBE_ID_RE.match(s) else None


def _coerce_string_list(value: object) -> list[str]:
    """Return value as a list of plain strings, dropping anything else: a tampered DB row could
    carry nested objects or ints in genres.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _distinct(names: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for name in names:
        seen.setdefault(name.strip().casefold(), name.strip())
    return [name for name in seen.values() if name]


def _coerce_cast(value: object) -> list[dict[str, str]]:
    """Return value as name and character pairs, dropping an entry with no plain-string name."""
    if not isinstance(value, list):
        return []
    members = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name, character = entry.get("name"), entry.get("character")
        if isinstance(name, str) and name:
            members.append(
                {"name": name, "character": character if isinstance(character, str) else ""}
            )
    return members


def _base_download_item(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "title": payload.get("title", ""),
        "media_type": payload.get("mt", ""),
        "tmdb_id": payload.get("tmdb"),
        "email": payload.get("email", ""),
        "action": payload.get("act", "download"),
        "poster_url": None,
        "backdrop_url": None,
        "year": None,
        "description": None,
        "reason": None,
        "rating": None,
        "rt_rating": None,
        "tagline": None,
        "runtime": None,
        "genres": None,
        "cast_json": None,
        "director": None,
        "trailer_key": None,
        "imdb_rating": None,
        "metascore": None,
        "genres_list": [],
        "cast_list": [],
        "season_number": None,
    }


def _build_item_from_suggestion(
    payload: Mapping[str, object], enrichment: SuggestionEnrichment
) -> dict[str, object]:
    item = _base_download_item(payload)
    item.update(
        {
            "poster_url": enrichment.poster_url,
            "year": enrichment.year,
            "description": enrichment.description,
            "reason": enrichment.reason,
            "rating": enrichment.rating,
            "rt_rating": enrichment.rt_rating,
            "tagline": enrichment.tagline,
            "runtime": enrichment.runtime,
            "genres": enrichment.genres,
            "cast_json": enrichment.cast_json,
            "director": enrichment.director,
            # Sanitisation is applied by _enrich_item_fields (always called
            # after item construction), so the raw value is stored here and
            # validated exactly once — not twice.
            "trailer_key": enrichment.trailer_key,
            "imdb_rating": enrichment.imdb_rating,
            "metascore": enrichment.metascore,
        }
    )
    return item


def _build_item_from_payload(
    payload: Mapping[str, object],
    conn: sqlite3.Connection,
    secret_key: str,
) -> dict[str, object]:
    if payload.get("act") == "redownload":
        item = _base_download_item(payload)
        enrich_redownload_item(item, conn, secret_key)
        return item
    sid = payload.get("sid")
    if sid:
        # ``sid`` is typed as ``int | None`` in DownloadTokenPayload but the
        # token producer is responsible for the type — coerce here so the
        # SQL parameter is always an int regardless of upstream regressions.
        try:
            sid_int = int(str(sid))
        except (TypeError, ValueError):
            sid_int = None
        if sid_int is not None:
            enrichment = fetch_suggestion_enrichment(conn, sid_int)
            if enrichment is not None:
                return _build_item_from_suggestion(payload, enrichment)
    # The link of a person's own pick names no stored row, so the title's own facts are
    # looked up. No reason comes with them: it would be one given for a shared pick.
    item = _base_download_item(payload)
    if payload.get("tmdb"):
        enrich_redownload_item(item, conn, secret_key)
        item["reason"] = None
    return item


def _enrich_item_fields(item: dict[str, object]) -> None:
    trailer_value = item.get("trailer_key")
    item["trailer_key"] = validate_youtube_id(
        trailer_value if isinstance(trailer_value, str) else None
    )
    genres_value = item.get("genres")
    if isinstance(genres_value, (str, bytes, bytearray)):
        try:
            item["genres_list"] = _distinct(_coerce_string_list(json.loads(genres_value)))
        except (json.JSONDecodeError, TypeError):
            item["genres_list"] = []
    cast_value = item.get("cast_json")
    if isinstance(cast_value, (str, bytes, bytearray)):
        try:
            item["cast_list"] = _coerce_cast(json.loads(cast_value))
        except (json.JSONDecodeError, TypeError):
            item["cast_list"] = []


def _season_state(series: SonarrSeries, season_number: int) -> str | None:
    """Like compute_download_state for one season: None when Sonarr holds the series but is
    not set to fetch the season.
    """
    for season in series.get("seasons") or []:
        if season.get("seasonNumber") != season_number:
            continue
        if season_on_disk(season):
            return ACTION_IN_LIBRARY
        if series.get("monitored", True) and season.get("monitored"):
            return ACTION_QUEUED
    return None


def _aired_missing(series: SonarrSeries) -> bool:
    """Whether Sonarr reports an aired episode it wants and lacks; a show just added, whose
    episodes it has yet to list, counts as lacking them.
    """
    reported = False
    for season in series.get("seasons") or []:
        stats: dict[str, Any] = {**(season.get("statistics") or {})}
        if stats.get("episodeCount", 0) > stats.get("episodeFileCount", 0):
            return True
        reported = reported or any(
            stats.get(k)
            for k in ("episodeCount", "totalEpisodeCount", "previousAiring", "nextAiring")
        )
    return not reported


def _resolve_download_state(
    item: dict[str, object],
    conn: sqlite3.Connection,
    tmdb_id: int | None,
    secret_key: str,
    mt: str,
) -> None:
    item["download_state"] = None
    if not tmdb_id:
        return
    try:
        # Only build the cache for the matching service — the other
        # half is dead work and doubles the outbound load otherwise.
        # Use ``.get`` with empty defaults so a malformed upstream
        # cache (e.g. a unit test that patches ``build_radarr_cache``
        # to return ``{}``) doesn't crash the page render.
        caches: ArrCaches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {},
            "sonarr_queue_tmdb_ids": set(),
        }
        if mt == "movie":
            radarr_cache = _get_radarr_cache_cached(conn, secret_key)
            caches["radarr_movies"] = radarr_cache.get("radarr_movies", {}) or {}
            caches["radarr_queue_tmdb_ids"] = radarr_cache.get("radarr_queue_tmdb_ids") or set()
        else:
            sonarr_cache = _get_sonarr_cache_cached(conn, secret_key)
            caches["sonarr_series"] = sonarr_cache.get("sonarr_series", {}) or {}
            caches["sonarr_queue_tmdb_ids"] = sonarr_cache.get("sonarr_queue_tmdb_ids") or set()
        season_number = item["season_number"]
        if isinstance(season_number, int):
            held = caches["sonarr_series"].get(tmdb_id)
            item["download_state"] = _season_state(held, season_number) if held else None
            return
        state = compute_download_state(mt, tmdb_id, caches)
        series = caches["sonarr_series"].get(tmdb_id) if mt != "movie" else None
        if (
            series is not None
            and state in (ACTION_PARTIAL, ACTION_QUEUED)
            and tmdb_id not in caches["sonarr_queue_tmdb_ids"]
        ):
            # What is not downloading is on its way only if Sonarr has something to look for.
            if not series.get("monitored", True):
                state = None
            elif _aired_missing(series):
                state = ACTION_QUEUED
            else:
                state = ACTION_IN_LIBRARY if series_has_files(series) else None
        # The page shows one "on its way" view, whose progress polling fills in.
        if state in (ACTION_DOWNLOADING, ACTION_PARTIAL):
            state = ACTION_QUEUED
        # A show Sonarr holds but has nothing to fetch for has no state: say it is tracked, not queued.
        if state is None and series is not None:
            state = "tracked"
        if state is not None:
            item["download_state"] = state
    except (requests.RequestException, SafeHTTPError, ArrError):
        logger.warning("Failed to check Arr library status for tmdb_id=%s", tmdb_id, exc_info=True)


def _build_hero_context(
    item: dict[str, object], tmdb_id: int | None, secret_key: str
) -> tuple[object, object]:
    """Build the hero item and poll token when the item is already queued.

    Returns (hero_item, poll_token); both are None when not applicable.
    """
    if item["download_state"] != "queued":
        return None, None
    service = "radarr" if item["media_type"] == "movie" else "sonarr"
    hero_title = cast(str, item["title"])
    hero_media_type = cast(str, item["media_type"])
    hero_poster = cast("str | None", item.get("poster_url")) or ""
    hero_item = build_item(
        dl_id=f"{service}:{hero_title}",
        title=hero_title,
        media_type=hero_media_type,
        poster_url=hero_poster,
        state="searching",
        progress=0,
        eta="",
        size_done="",
        size_total="",
    )
    # Mint a short-lived poll token so the page can start polling immediately
    # without exposing the long-lived download token to the polling endpoint.
    poll_token = None
    if tmdb_id:
        poll_token = generate_poll_token(
            media_item_id=(
                sonarr_media_id(hero_title, cast("int | None", item["season_number"]))
                if service == "sonarr"
                else f"radarr:{hero_title}"
            ),
            service=service,
            tmdb_id=tmdb_id,
            secret_key=secret_key,
        )
    return hero_item, poll_token


@router.get("/download/{token}", response_class=HTMLResponse)
def download_page(request: Request, token: str) -> HTMLResponse:
    config = request.app.state.config
    templates = cast(Jinja2Templates, request.app.state.templates)
    conn = get_db()

    if not _DOWNLOAD_LIMITER_GET.check(get_client_ip(request)):
        limited = templates.TemplateResponse(
            request, "download.html", {"state": "limited", "item": None}
        )
        limited.status_code = 429
        limited.headers["Retry-After"] = "60"
        return limited

    _expired = templates.TemplateResponse(
        request,
        "download.html",
        {"state": "expired", "item": None},
    )

    if len(token) > 4096:
        return _expired

    payload = validate_download_token(token, config.secret_key)
    if payload is None:
        return _expired

    item = _build_item_from_payload(payload, conn, config.secret_key)
    _enrich_item_fields(item)
    item["description"] = tidy_summary(cast("str | None", item["description"]))
    fill_backdrop(item, conn, config.secret_key)
    season = deleted_season(conn, payload)
    item["season_number"] = season.season_number if season else None

    mt = "movie" if payload.get("mt") == "movie" else "tv"
    tmdb_id_raw = payload.get("tmdb")
    tmdb_id: int | None = int(tmdb_id_raw) if isinstance(tmdb_id_raw, int) else None
    _resolve_download_state(item, conn, tmdb_id, config.secret_key, mt)

    item["notify_by_email"] = bool(item["email"])
    if item["download_state"] == ACTION_QUEUED and item["email"] and tmdb_id:
        # The button is gone, so nobody can ask for the email any more: promise it only if asked for.
        item["notify_by_email"] = has_unsent_notification(
            conn,
            email=str(item["email"]),
            service="radarr" if mt == "movie" else "sonarr",
            tmdb_id=tmdb_id,
            tvdb_id=season.tvdb_id if season else None,
            season_number=season.season_number if season else None,
        )

    hero_item, poll_token = _build_hero_context(item, tmdb_id, config.secret_key)

    return templates.TemplateResponse(
        request,
        "download.html",
        {
            "state": "confirm",
            "item": item,
            "token": token,
            "poll_token": poll_token,
            "hero_item": hero_item,
            "hero_backdrop": item["backdrop_url"],
        },
    )
