"""Download confirmation page."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from typing import cast

import requests
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mediaman.crypto import generate_poll_token, validate_download_token
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
from mediaman.services.downloads.download_format import build_item
from mediaman.services.infra.http_client import SafeHTTPError
from mediaman.services.media_meta.item_enrichment import enrich_redownload_item
from mediaman.services.rate_limit import RateLimiter, get_client_ip

# YouTube video IDs are exactly 11 URL-safe base64 characters.
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

logger = logging.getLogger("mediaman")

router = APIRouter()

# Rate limiter for the public download GET endpoint.
_DOWNLOAD_LIMITER_GET = RateLimiter(max_attempts=30, window_seconds=60)

# ---------------------------------------------------------------------------
# Per-service Arr-state cache.
#
# Each GET /download/{token} previously issued four outbound HTTP calls
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
# ---------------------------------------------------------------------------

_ARR_CACHE_TTL_SECONDS = 30.0
_ARR_CACHE_LOCK = threading.Lock()
# (service_name, secret_key_fingerprint) -> (timestamp, cache_payload).
# The payload is either a RadarrCaches or SonarrCaches TypedDict; the
# caller knows which one to expect from the service tag in the key.
_ARR_CACHE: dict[tuple[str, str], tuple[float, RadarrCaches | SonarrCaches]] = {}


def _key_fingerprint(secret_key: str) -> str:
    """Short fingerprint of *secret_key* for use as a cache key.

    The full key never appears in the cache; only its first 8 bytes of a
    hash. Different deployments with different secrets do not collide.
    """
    import hashlib

    return hashlib.sha256(secret_key.encode()).hexdigest()[:16]


def _get_radarr_cache_cached(conn, secret_key: str) -> RadarrCaches:
    """Return the Radarr cache dict, using a process-wide TTL cache."""
    key = ("radarr", _key_fingerprint(secret_key))
    now = time.monotonic()
    with _ARR_CACHE_LOCK:
        hit = _ARR_CACHE.get(key)
        if hit and now - hit[0] < _ARR_CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]
    radarr_client = build_radarr_from_db(conn, secret_key)
    cache = build_radarr_cache(radarr_client)
    with _ARR_CACHE_LOCK:
        _ARR_CACHE[key] = (now, cache)
    return cache


def _get_sonarr_cache_cached(conn, secret_key: str) -> SonarrCaches:
    """Return the Sonarr cache dict, using a process-wide TTL cache."""
    key = ("sonarr", _key_fingerprint(secret_key))
    now = time.monotonic()
    with _ARR_CACHE_LOCK:
        hit = _ARR_CACHE.get(key)
        if hit and now - hit[0] < _ARR_CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]
    sonarr_client = build_sonarr_from_db(conn, secret_key)
    cache = build_sonarr_cache(sonarr_client)
    with _ARR_CACHE_LOCK:
        _ARR_CACHE[key] = (now, cache)
    return cache


def _reset_arr_cache_for_tests() -> None:
    """Clear the Arr-state cache. Test helper; never call in production."""
    with _ARR_CACHE_LOCK:
        _ARR_CACHE.clear()


def validate_youtube_id(s: str | None) -> str | None:
    """Return *s* if it is a valid YouTube video ID, else None."""
    if not s:
        return None
    return s if _YOUTUBE_ID_RE.match(s) else None


# Backward-compat alias.
_validate_youtube_id = validate_youtube_id


def _coerce_string_list(value: object) -> list[str]:
    """Return *value* coerced to a list of plain strings.

    Defends against malicious or malformed JSON in DB columns: a
    suggestion's ``genres`` / ``cast_json`` should be a list of strings,
    but a tampered DB row could carry nested objects, ints, or arbitrary
    JSON. The template treats the result as plain text, so anything
    that cannot be a string is dropped.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _base_download_item(payload: Mapping[str, object]) -> dict[str, object]:
    """Build the skeleton download item from a validated token payload."""
    return {
        "title": payload.get("title", ""),
        "media_type": payload.get("mt", ""),
        "tmdb_id": payload.get("tmdb"),
        "email": payload.get("email", ""),
        "action": payload.get("act", "download"),
        "poster_url": None,
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
    }


def _build_item_from_suggestion(
    payload: Mapping[str, object], row: sqlite3.Row
) -> dict[str, object]:
    """Build a fully-populated download item from a suggestions DB row."""
    item = _base_download_item(payload)
    item.update(
        {
            "poster_url": row["poster_url"],
            "year": row["year"],
            "description": row["description"],
            "reason": row["reason"],
            "rating": row["rating"],
            "rt_rating": row["rt_rating"],
            "tagline": row["tagline"],
            "runtime": row["runtime"],
            "genres": row["genres"],
            "cast_json": row["cast_json"],
            "director": row["director"],
            "trailer_key": validate_youtube_id(row["trailer_key"]),
            "imdb_rating": row["imdb_rating"],
            "metascore": row["metascore"],
        }
    )
    return item


@router.get("/download/{token}", response_class=HTMLResponse)
def download_page(request: Request, token: str) -> HTMLResponse:
    """Render the download confirmation page."""
    config = request.app.state.config
    templates = request.app.state.templates
    conn = get_db()

    if not _DOWNLOAD_LIMITER_GET.check(get_client_ip(request)):
        return HTMLResponse("Too many requests. Try again later.", status_code=429)

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

    sid = payload.get("sid")
    if sid:
        # ``sid`` is typed as ``int | None`` in DownloadTokenPayload but the
        # token producer is responsible for the type — coerce here so the
        # SQL parameter is always an int regardless of upstream regressions.
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            sid_int = None
        if sid_int is not None:
            row = conn.execute(
                "SELECT poster_url, year, description, reason, rating, rt_rating, "
                "tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore "
                "FROM suggestions WHERE id = ?",
                (sid_int,),
            ).fetchone()
            item = (
                _build_item_from_suggestion(payload, row) if row else _base_download_item(payload)
            )
        else:
            item = _base_download_item(payload)
    elif payload.get("act") == "redownload":
        item = _base_download_item(payload)
        enrich_redownload_item(item, conn, config.secret_key)
    else:
        item = _base_download_item(payload)

    trailer_value = item.get("trailer_key")
    item["trailer_key"] = validate_youtube_id(
        trailer_value if isinstance(trailer_value, str) else None
    )

    genres_value = item.get("genres")
    if isinstance(genres_value, (str, bytes, bytearray)):
        try:
            item["genres_list"] = _coerce_string_list(json.loads(genres_value))
        except (json.JSONDecodeError, TypeError):
            item["genres_list"] = []
    cast_value = item.get("cast_json")
    if isinstance(cast_value, (str, bytes, bytearray)):
        try:
            item["cast_list"] = _coerce_string_list(json.loads(cast_value))
        except (json.JSONDecodeError, TypeError):
            item["cast_list"] = []

    item["download_state"] = None
    tmdb_id = payload.get("tmdb")
    if tmdb_id:
        try:
            mt = "movie" if payload.get("mt") == "movie" else "tv"
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
                radarr_cache = _get_radarr_cache_cached(conn, config.secret_key)
                caches["radarr_movies"] = radarr_cache.get("radarr_movies", {}) or {}
                caches["radarr_queue_tmdb_ids"] = radarr_cache.get("radarr_queue_tmdb_ids") or set()
            else:
                sonarr_cache = _get_sonarr_cache_cached(conn, config.secret_key)
                caches["sonarr_series"] = sonarr_cache.get("sonarr_series", {}) or {}
                caches["sonarr_queue_tmdb_ids"] = sonarr_cache.get("sonarr_queue_tmdb_ids") or set()
            state = compute_download_state(mt, tmdb_id, caches)
            if state is not None:
                item["download_state"] = state
        except (requests.RequestException, SafeHTTPError):
            logger.warning(
                "Failed to check Arr library status for tmdb_id=%s", tmdb_id, exc_info=True
            )

    hero_item = None
    poll_token = None
    if item["download_state"] == "queued":
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
        # Finding 14: mint a short-lived poll token so the page can start
        # polling immediately without exposing the long-lived download token.
        if tmdb_id:
            poll_token = generate_poll_token(
                media_item_id=f"{service}:{hero_title}",
                service=service,
                tmdb_id=tmdb_id,
                secret_key=config.secret_key,
            )

    return templates.TemplateResponse(
        request,
        "download.html",
        {
            "state": "confirm",
            "item": item,
            "token": token,
            "poll_token": poll_token,
            "hero_item": hero_item,
        },
    )
