"""Download status polling endpoint (GET /api/download/status).

The _radarr_status and _sonarr_status orchestrators stay here, not in the projection
modules, so tests can patch build_radarr_from_db and build_sonarr_from_db at this path. The
pure projections live in ._shared, ._radarr and ._sonarr.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Literal

import requests
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from mediaman.crypto import generate_poll_token, validate_poll_token
from mediaman.db import get_db
from mediaman.services.arr import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.download_format import build_item
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.infra import SafeHTTPError
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.auth.middleware import get_optional_admin
from mediaman.web.responses import respond_err
from mediaman.web.routes.download._cache_keys import _key_fingerprint
from mediaman.web.routes.download._season import season_of_media_id

from ._radarr import _radarr_queue_item, _radarr_ready_item, _radarr_searching_item

# ``_format_timeleft`` is re-exported from ``._shared`` because the status
# unit tests import it from this module path; the ``as`` form marks the
# re-export as deliberate.
from ._shared import _format_timeleft as _format_timeleft
from ._sonarr import _sonarr_aggregate, _sonarr_queue_entries, _sonarr_series_fallback

logger = logging.getLogger(__name__)

router = APIRouter()

# Rate-limiter is process-scoped: per-IP counters must persist across requests
# in the same worker to enforce the rolling window correctly.
_DOWNLOAD_STATUS_LIMITER = RateLimiter(max_attempts=120, window_seconds=60)

# Per-service per-tmdb status cache.
#
# The status endpoint is polled by the confirm page every ~4 seconds while
# the user watches a download progress bar. Each poll fans out to Radarr or
# Sonarr (a queue lookup plus a movie / series lookup), so a single open
# tab on a 4-second poll already keeps a sustained outbound load on the
# operator's home Arr boxes; multiple tabs multiply it. Cache the
# per-(service, tmdb_id) status for a short window so back-to-back polls
# from the same client share one upstream round-trip.
#
# TTL is intentionally short (2.5s) so the user still sees fresh progress
# on the 4-second poll cadence — half the poll interval ensures every
# poll either hits a fresh fetch or a result that was at most one beat
# stale. Anything longer is still bounded but reduces apparent
# responsiveness; anything shorter does not meaningfully reduce load.

_STATUS_CACHE_TTL_SECONDS = 2.5
# rationale: bounded to prevent unbounded growth — with many unique
# (service, tmdb_id) pairs under sustained load the dict would otherwise
# grow without limit; max 200 entries with clear-on-overflow is the
# safety net (TTL alone only evicts on hit, not proactively).
_STATUS_CACHE_MAX_ENTRIES = 200
_STATUS_CACHE_LOCK = threading.Lock()
# Per-process per-(service, tmdb_id) status cache; must be in-process state
# so the short TTL reduces outbound amplification for unauthenticated polls
# across requests handled by the same worker.
# (service, tmdb_id, season, secret_key_fingerprint) -> (timestamp, status_dict)
_STATUS_CACHE: dict[tuple[str, int, int | None, str], tuple[float, DownloadItem]] = {}

# A poll token with less than this many seconds left (half its default
# 600-second life) is replaced in the status response, so a page left open
# on a long download keeps polling instead of freezing at the token's expiry.
_POLL_TOKEN_REFRESH_SECONDS = 300

# Renewal stops this long after the first token of a chain was issued, so a
# held token cannot be kept alive forever while a download never finishes.
_POLL_TOKEN_MAX_CHAIN_SECONDS = 24 * 3600

# Life of a poll token issued before tokens carried their first-issue time;
# subtracting it from ``exp`` recovers when such a token was issued.
_LEGACY_POLL_TOKEN_TTL_SECONDS = 600


def _reset_status_cache_for_tests() -> None:
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.clear()


_UNKNOWN_ITEM: DownloadItem = build_item(
    dl_id="",
    title="",
    media_type="movie",
    poster_url="",
    state="unknown",
    progress=0,
    eta="",
    size_done="",
    size_total="",
)


def _radarr_status(conn: sqlite3.Connection, secret_key: str, tmdb_id: int) -> DownloadItem:
    client = build_radarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

    movie = client.get_movie_by_tmdb(tmdb_id)
    if movie and movie.get("hasFile"):
        return _radarr_ready_item(movie)

    queued = _radarr_queue_item(client.get_queue(), tmdb_id)
    if queued is not None:
        return queued

    return _radarr_searching_item(movie)


def _sonarr_status(
    conn: sqlite3.Connection, secret_key: str, tmdb_id: int, season_number: int | None = None
) -> DownloadItem:
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

    queue = client.get_queue()
    series_title, series_poster, ep_entries = _sonarr_queue_entries(queue, tmdb_id, season_number)

    if ep_entries:
        return _sonarr_aggregate(series_title, series_poster, ep_entries)

    return _sonarr_series_fallback(client, tmdb_id, season_number)


def _cached_status(
    *,
    service: str,
    tmdb_id: int,
    season_number: int | None,
    conn: sqlite3.Connection,
    secret_key: str,
) -> DownloadItem:
    key = (service, tmdb_id, season_number, _key_fingerprint(secret_key))
    now = time.monotonic()
    with _STATUS_CACHE_LOCK:
        hit = _STATUS_CACHE.get(key)
        if hit and now - hit[0] < _STATUS_CACHE_TTL_SECONDS:
            return hit[1]
    if service == "radarr":
        result = _radarr_status(conn, secret_key, tmdb_id)
    else:
        result = _sonarr_status(conn, secret_key, tmdb_id, season_number)
    with _STATUS_CACHE_LOCK:
        if len(_STATUS_CACHE) >= _STATUS_CACHE_MAX_ENTRIES:
            # Evict expired entries first; if still full, clear wholesale.
            cutoff = now - _STATUS_CACHE_TTL_SECONDS
            stale = [k for k, (ts, _) in _STATUS_CACHE.items() if ts < cutoff]
            for k in stale:
                del _STATUS_CACHE[k]
            if len(_STATUS_CACHE) >= _STATUS_CACHE_MAX_ENTRIES:
                _STATUS_CACHE.clear()
        _STATUS_CACHE[key] = (now, result)
    return result


@router.get("/api/download/status")
def download_status(
    request: Request,
    service: Literal["radarr", "sonarr"],
    tmdb_id: int = Query(..., gt=0),
    poll_token: str | None = None,
    admin: str | None = Depends(get_optional_admin),
) -> JSONResponse:
    """Poll download progress for a recently requested item.

    Unauthenticated callers must supply a poll_token (short-lived, service/tmdb-bound) from
    the submit endpoint; admins may poll without one. service and tmdb_id are constrained so
    malformed callers get a 422 instead of a placeholder response.
    """
    config = request.app.state.config

    if not _DOWNLOAD_STATUS_LIMITER.check(get_client_ip(request)):
        limited = respond_err("too_many_requests", status=429)
        limited.headers["Retry-After"] = "60"
        return limited

    poll_payload = None
    if poll_token is not None and len(poll_token) <= 4096:
        poll_payload = validate_poll_token(poll_token, config.secret_key)
        if poll_payload is not None and (
            poll_payload.get("svc") != service or poll_payload.get("tmdb") != tmdb_id
        ):
            poll_payload = None
    poll_media_item_id = poll_payload["mid"] if poll_payload else ""
    season_number = season_of_media_id(poll_media_item_id)

    renewal_due = False
    poll_first_issued = 0
    if not admin:
        # Unauthenticated polling must use a short-lived poll_token.  The
        # long-lived download token is not accepted for status polling — it
        # is single-use and only valid for the /download/{token} POST.
        # Clients receive a poll_token in the submit response and must use
        # it exclusively for polling.
        if poll_payload is None:
            return respond_err("not_authenticated", status=401)

        now = time.time()
        if poll_payload["exp"] - now < _POLL_TOKEN_REFRESH_SECONDS:
            poll_first_issued = int(
                poll_payload.get("first", poll_payload["exp"] - _LEGACY_POLL_TOKEN_TTL_SECONDS)
            )
            renewal_due = now - poll_first_issued < _POLL_TOKEN_MAX_CHAIN_SECONDS

    conn = get_db()

    try:
        # The cache exists to bound the request-amplification attack
        # surface of unauthenticated polls (a public link with a
        # poll_token times the rate limit). Admins are already trusted
        # and rate-limited; bypassing the cache for them keeps the
        # admin UI showing fresh data without the slight staleness an
        # admin tab would otherwise see, and avoids stale-cache
        # surprises in admin-driven test suites.
        if admin:
            if service == "radarr":
                result = _radarr_status(conn, config.secret_key, tmdb_id)
            else:
                result = _sonarr_status(conn, config.secret_key, tmdb_id, season_number)
        else:
            result = _cached_status(
                service=service,
                tmdb_id=tmdb_id,
                season_number=season_number,
                conn=conn,
                secret_key=config.secret_key,
            )
    except (requests.RequestException, SafeHTTPError, ArrError):
        # Both transport (RequestException) and Arr-level non-2xx
        # (SafeHTTPError) failures are bounded behaviours we report as
        # "unknown" rather than 500. Without SafeHTTPError in this clause
        # a Radarr/Sonarr 500 would propagate as an unhandled exception
        # and surface as a 500 to the polling client.
        logger.exception("download_status error (service=%s tmdb_id=%s)", service, tmdb_id)
        result = _UNKNOWN_ITEM

    # Renew only while the download is unfinished and the chain is under 24
    # hours old, so a held token cannot be kept alive forever; an unknown
    # state (Arr down) still renews until the cap.
    if renewal_due and result.get("state") != "ready":
        fresh_poll_token = generate_poll_token(
            media_item_id=poll_media_item_id,
            service=service,
            tmdb_id=tmdb_id,
            secret_key=config.secret_key,
            first_issued=poll_first_issued,
        )
        # A new dict, so the shared cache entry never carries one visitor's
        # token to another.
        return JSONResponse({**result, "poll_token": fresh_poll_token})
    return JSONResponse(result)
