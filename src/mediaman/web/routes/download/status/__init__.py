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
from mediaman.services.arr.finding_copy import is_finding_copy, will_retry
from mediaman.services.downloads.download_format import (
    attention_reason,
    build_item,
    map_queue_state,
)
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.downloads.notifications import requested_tvdb_id
from mediaman.services.downloads.strip import WAITING_LINE
from mediaman.services.infra import SafeHTTPError
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.auth.middleware import get_optional_admin
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.responses import respond_err
from mediaman.web.routes.download._cache_keys import _key_fingerprint
from mediaman.web.routes.download._season import season_of_media_id
from mediaman.web.routes.downloads_strip import library_of, library_title

from ._radarr import _radarr_queue_item, _radarr_ready_item, _radarr_searching_item

# ``_format_timeleft`` is re-exported from ``._shared`` because the status
# unit tests import it from this module path; the ``as`` form marks the
# re-export as deliberate.
from ._shared import _format_timeleft as _format_timeleft
from ._sonarr import (
    _is_series,
    _sonarr_aggregate,
    _sonarr_queue_entries,
    _sonarr_series_fallback,
)

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
        ready = _radarr_ready_item(movie)
        return _playable(conn, secret_key, ready, "movie", tmdb_id, movie.get("year"))

    queue = client.get_queue()
    queued = _radarr_queue_item(queue, tmdb_id)
    movie_id = movie.get("id") if movie else None
    if queued is not None:
        records = [q for q in queue if (q.get("movie") or {}).get("tmdbId") == tmdb_id]
        states = [
            {
                "status": q.get("status") or "",
                "tracked_state": q.get("trackedDownloadState") or "",
                "tracked_status": q.get("trackedDownloadStatus") or "",
            }
            for q in records
        ]
        # Nothing is tried again while another copy of the film still comes in.
        held = map_queue_state(states) == "needs_attention"
        key = f"radarr:{movie_id}" if isinstance(movie_id, int) and held else None
        return _retrying(conn, queued, key, [attention_reason(q) for q in records])

    return _radarr_searching_item(
        movie, finding_copy=isinstance(movie_id, int) and is_finding_copy(conn, "radarr", movie_id)
    )


def _sonarr_status(
    conn: sqlite3.Connection, secret_key: str, tmdb_id: int, season_number: int | None = None
) -> DownloadItem:
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

    queue = client.get_queue()
    tvdb_id = requested_tvdb_id(conn, tmdb_id)
    series_title, ep_entries = _sonarr_queue_entries(queue, tmdb_id, season_number, tvdb_id)

    if ep_entries:
        series_id = next(
            (
                (item.get("series") or {}).get("id")
                for item in queue
                if _is_series(item.get("series") or {}, tmdb_id, tvdb_id)
            ),
            None,
        )
        season = "" if season_number is None else season_number
        key = f"sonarr:{series_id}:{season}" if isinstance(series_id, int) else None
        found = _sonarr_aggregate(series_title, ep_entries)
        return _retrying(conn, found, key, [e["attention"] for e in ep_entries])

    found = _sonarr_series_fallback(
        client,
        tmdb_id,
        season_number,
        tvdb_id,
        finding_copy=lambda series_id: is_finding_copy(conn, "sonarr", series_id, season_number),
    )
    return _playable(conn, secret_key, found, "tv", tmdb_id)


def _retrying(
    conn: sqlite3.Connection, item: DownloadItem, key: str | None, reasons: list[str]
) -> DownloadItem:
    """*item*, of one that needs attention saying whether another copy will be looked for."""
    if item["state"] != "needs_attention":
        return item
    return {**item, "retrying": key is not None and will_retry(conn, key, reasons)}


def _playable(
    conn: sqlite3.Connection,
    secret_key: str,
    item: DownloadItem,
    kind: str,
    tmdb_id: int,
    year: int | None = None,
) -> DownloadItem:
    """*item*, ready only once the library holds its title, as Downloads and the strip say;
    until then almost ready, and said to be on its way to the library."""
    if item["state"] != "ready":
        return item
    if library_title(library_of(conn, secret_key), kind, tmdb_id, item["title"], year) is not None:
        return item
    return {**item, "state": "almost_ready", "state_label": WAITING_LINE}


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
    if not admin or get_user_role(conn, admin) != "admin":
        # What Radarr or Sonarr says is wrong is an admin's to read and act on.
        result = {**result, "arr_source": "", "attention": "", "attention_hint": ""}

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
