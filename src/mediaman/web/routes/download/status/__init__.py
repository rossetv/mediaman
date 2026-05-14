"""Download status polling endpoint.

This package is the ``GET /api/download/status`` route. The barrel keeps the
handler, the per-IP rate limiter, the short-TTL status cache, and the
``_radarr_status`` / ``_sonarr_status`` orchestrators — the orchestrators
stay here (rather than in the projection modules) so the
``build_radarr_from_db`` / ``build_sonarr_from_db`` call sites remain
patchable by the test suite at ``...download.status.build_*_from_db``.

The pure status projections — turning Radarr / Sonarr payloads into the
``DownloadItem`` envelope — live in the private submodules:

  * :mod:`._shared`  — numeric coercion + ``_format_timeleft`` formatting.
  * :mod:`._radarr`  — Radarr movie / queue projections.
  * :mod:`._sonarr`  — Sonarr queue-walk, multi-episode aggregation, and the
    no-queue ``get_series()`` fallback.

``router``, ``_DOWNLOAD_STATUS_LIMITER`` and ``_reset_status_cache_for_tests``
are imported by the parent ``download`` package barrel; ``_radarr_status``,
``_sonarr_status`` and ``_format_timeleft`` are exercised directly by the
tests — all stay importable from this module path.
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

from mediaman.crypto import validate_poll_token
from mediaman.db import get_db
from mediaman.services.arr import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.download_format import build_item
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.infra import SafeHTTPError
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.auth.middleware import get_optional_admin
from mediaman.web.responses import respond_err

from ._radarr import _radarr_fallback_item, _radarr_queue_item, _radarr_ready_item

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

# ---------------------------------------------------------------------------
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
# ---------------------------------------------------------------------------

_STATUS_CACHE_TTL_SECONDS = 2.5
_STATUS_CACHE_LOCK = threading.Lock()

# (service, tmdb_id, secret_key_fingerprint) -> (timestamp, status_dict)
_STATUS_CACHE: dict[tuple[str, int, str], tuple[float, DownloadItem]] = {}


def _key_fingerprint(secret_key: str) -> str:
    """Short fingerprint of *secret_key* for use as a cache key."""
    import hashlib

    return hashlib.sha256(secret_key.encode()).hexdigest()[:16]


def _reset_status_cache_for_tests() -> None:
    """Clear the status cache. Test helper; never call in production."""
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
    """Return the download-status item dict for a Radarr movie by TMDB ID."""
    client = build_radarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

    movie = client.get_movie_by_tmdb(tmdb_id)
    if movie and movie.get("hasFile"):
        return _radarr_ready_item(movie)

    queued = _radarr_queue_item(client.get_queue(), tmdb_id)
    if queued is not None:
        return queued

    return _radarr_fallback_item(conn, movie)


def _sonarr_status(conn: sqlite3.Connection, secret_key: str, tmdb_id: int) -> DownloadItem:
    """Return the download-status item dict for a Sonarr series by TMDB ID.

    Runs the three Sonarr projection phases from :mod:`._sonarr` in order:
    walk the queue for matching episodes, and either aggregate those into a
    multi-episode item or — when the queue holds nothing — fall back to the
    ``get_series()`` on-disk / recent-download / searching classification.
    """
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

    queue = client.get_queue()
    series_title, series_poster, ep_entries = _sonarr_queue_entries(queue, tmdb_id)

    if ep_entries:
        return _sonarr_aggregate(series_title, series_poster, ep_entries)

    return _sonarr_series_fallback(client, conn, tmdb_id)


def _cached_status(
    *, service: str, tmdb_id: int, conn: sqlite3.Connection, secret_key: str
) -> DownloadItem:
    """Return cached or freshly-fetched status for ``(service, tmdb_id)``.

    See module-level docstring for cache semantics. Cache misses fan
    out to the underlying ``_radarr_status`` / ``_sonarr_status``
    implementation; cache hits short-circuit the upstream call entirely.
    """
    key = (service, tmdb_id, _key_fingerprint(secret_key))
    now = time.monotonic()
    with _STATUS_CACHE_LOCK:
        hit = _STATUS_CACHE.get(key)
        if hit and now - hit[0] < _STATUS_CACHE_TTL_SECONDS:
            return hit[1]
    if service == "radarr":
        result = _radarr_status(conn, secret_key, tmdb_id)
    else:
        result = _sonarr_status(conn, secret_key, tmdb_id)
    with _STATUS_CACHE_LOCK:
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
    """Poll the download progress for a recently-requested item.

    Unauthenticated callers must supply a ``poll_token`` (short-lived,
    service/tmdb-bound) returned by the submit endpoint.
    Authenticated admins may poll without a token.

    ``service`` is constrained to the literal set ``{"radarr", "sonarr"}``
    and ``tmdb_id`` must be a positive integer (TMDB IDs are 1-indexed),
    so malformed callers receive a 422 from FastAPI rather than reaching
    the handler with garbage values that would silently fall through to
    a placeholder response.
    """
    config = request.app.state.config

    if not _DOWNLOAD_STATUS_LIMITER.check(get_client_ip(request)):
        return respond_err("too_many_requests", status=429)

    if not admin:
        # Unauthenticated polling must use a short-lived poll_token.  The
        # long-lived download token is not accepted for status polling — it
        # is single-use and only valid for the /download/{token} POST.
        # Clients receive a poll_token in the submit response and must use
        # it exclusively for polling.
        authenticated = False

        if poll_token is not None and len(poll_token) <= 4096:
            poll_payload = validate_poll_token(poll_token, config.secret_key)
            if (
                poll_payload is not None
                and poll_payload.get("svc") == service
                and poll_payload.get("tmdb") == tmdb_id
            ):
                authenticated = True

        if not authenticated:
            return respond_err("not_authenticated", status=401)

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
                result = _sonarr_status(conn, config.secret_key, tmdb_id)
        else:
            result = _cached_status(
                service=service, tmdb_id=tmdb_id, conn=conn, secret_key=config.secret_key
            )
        return JSONResponse(result)

    except (requests.RequestException, SafeHTTPError, ArrError):
        # Both transport (RequestException) and Arr-level non-2xx
        # (SafeHTTPError) failures are bounded behaviours we report as
        # "unknown" rather than 500. Without SafeHTTPError in this clause
        # a Radarr/Sonarr 500 would propagate as an unhandled exception
        # and surface as a 500 to the polling client.
        logger.exception("download_status error (service=%s tmdb_id=%s)", service, tmdb_id)
        return JSONResponse(_UNKNOWN_ITEM)
