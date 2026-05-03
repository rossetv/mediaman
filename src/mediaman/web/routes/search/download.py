"""Download submission endpoint for the search experience.

Hosts ``POST /api/search/download`` plus the supporting per-admin / per-IP
rate limiters and short-window duplicate-request suppression. The body
schema (``_DownloadRequest``) lives here too because it is exclusive to
this endpoint.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Literal

import requests as _requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter, RateLimiter, get_client_ip
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.notifications import record_download_notification as _record_dn
from mediaman.services.infra.http_client import SafeHTTPError

logger = logging.getLogger("mediaman")

# ---------------------------------------------------------------------------
# Rate limiters for POST /api/search/download (finding 33)
# ---------------------------------------------------------------------------

# Per-admin burst limiter: 20 downloads per minute, 200 per day.
_DOWNLOAD_ADMIN_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=200,
)

# Per-IP limiter: 30 downloads per minute (covers unauthenticated / shared sessions).
_DOWNLOAD_IP_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)

# Duplicate-request suppression: block identical (username, tmdb_id, media_type, seasons)
# submissions within a short window to prevent accidental double-clicks from adding
# duplicates. The season set is included in the dedup key so a TV submission for
# seasons {1,2} does not block a follow-up submission for season {3} on the same
# series (finding 5).
_DOWNLOAD_DEDUP_WINDOW_SECONDS = 10.0
_download_dedup: dict[tuple[str, int, str, str], float] = {}
_download_dedup_lock = threading.Lock()


def _seasons_dedup_token(
    monitored_seasons: list[int] | None,
    search_seasons: list[int] | None,
) -> str:
    """Return a stable deterministic token for the season selection.

    Treats ``None`` (all seasons) and an empty list distinctly so the
    "monitor everything" submission can't collide with a "monitor only
    season 1" request. Sorting both lists makes the token order-stable
    regardless of how the client serialised them.
    """
    mon = "*" if monitored_seasons is None else ",".join(str(s) for s in sorted(monitored_seasons))
    srch = "*" if search_seasons is None else ",".join(str(s) for s in sorted(search_seasons))
    return f"m={mon};s={srch}"


def _is_duplicate_download(
    username: str,
    tmdb_id: int,
    media_type: str,
    seasons_token: str = "",
) -> bool:
    """Return True if an identical request was submitted within the dedup window.

    Cleans up stale entries on each call to bound memory growth.
    """
    key = (username, tmdb_id, media_type, seasons_token)
    now = time.monotonic()
    with _download_dedup_lock:
        # Prune stale entries.
        stale = [
            k for k, ts in _download_dedup.items() if now - ts > _DOWNLOAD_DEDUP_WINDOW_SECONDS
        ]
        for k in stale:
            _download_dedup.pop(k, None)
        if key in _download_dedup:
            return True
        _download_dedup[key] = now
        return False


class _DownloadRequest(BaseModel):
    """Body schema for ``POST /api/search/download`` (finding 3).

    ``extra="forbid"`` means an unknown key from the client raises HTTP 422
    rather than being silently ignored. ``media_type`` is constrained to
    the two values the route actually handles, the title is bounded to
    256 chars, and the season lists are capped at 100 entries — generous
    for any real series but tight enough to refuse a flood payload.
    """

    model_config = ConfigDict(extra="forbid")

    media_type: Literal["movie", "tv"]
    tmdb_id: int = Field(ge=1)
    title: str = Field(max_length=256)
    monitored_seasons: list[int] | None = Field(default=None, max_length=100)
    search_seasons: list[int] | None = Field(default=None, max_length=100)


# ``admin_users`` has no ``email`` column — the username is the only
# identifier for the admin account, so it is intentionally re-used as
# the notification ``email`` field on download_notifications rows. The
# notification subsystem treats this as an opaque identifier (it does
# not actually attempt SMTP delivery to a non-email value), but the
# coupling is documented here so a future schema migration to a real
# email column does not quietly leave this call site stale (finding 7).
def _resolve_admin_email(admin: str) -> str:
    """Return the notification identifier for *admin*.

    Currently this is just the admin username; see the module-level
    note above for why this is intentional.
    """
    return admin


router = APIRouter()


@router.post("/api/search/download")
def api_download(
    body: _DownloadRequest, request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    # Per-admin rate check.
    if not _DOWNLOAD_ADMIN_LIMITER.check(admin):
        logger.warning("search.download_throttled user=%s", admin)
        return JSONResponse(
            {"ok": False, "error": "Too many download requests — slow down"},
            status_code=429,
        )

    # Per-IP rate check.
    client_ip = get_client_ip(request)
    if not _DOWNLOAD_IP_LIMITER.check(client_ip):
        logger.warning("search.download_ip_throttled ip=%s", client_ip)
        return JSONResponse(
            {"ok": False, "error": "Too many download requests from this IP — slow down"},
            status_code=429,
        )

    # Duplicate-request suppression: same (admin, tmdb_id, media_type, seasons)
    # within the dedup window indicates a double-click or replay (finding 5).
    seasons_token = _seasons_dedup_token(body.monitored_seasons, body.search_seasons)
    if _is_duplicate_download(admin, body.tmdb_id, body.media_type, seasons_token):
        logger.info(
            "search.download_duplicate_suppressed user=%s tmdb=%s type=%s seasons=%s",
            admin,
            body.tmdb_id,
            body.media_type,
            seasons_token,
        )
        return JSONResponse(
            {"ok": False, "error": "Duplicate request — wait a moment before retrying"},
            status_code=429,
        )

    conn = get_db()
    secret_key = request.app.state.config.secret_key

    # Notification recipient: see ``_resolve_admin_email`` for the
    # rationale on re-using the admin username (finding 7).
    notify_email = _resolve_admin_email(admin)

    if body.media_type == "movie":
        radarr = build_radarr_from_db(conn, secret_key)
        if not radarr:
            return JSONResponse({"ok": False, "error": "Radarr not configured"}, status_code=503)
        try:
            if radarr.get_movie_by_tmdb(body.tmdb_id):
                return JSONResponse(
                    {"ok": False, "error": f"'{body.title}' is already in your library"},
                    status_code=409,
                )
            radarr.add_movie(body.tmdb_id, body.title)
        except (SafeHTTPError, _requests.RequestException, ValueError):
            # Narrow exception list (finding 6): SafeHTTPError covers
            # Radarr's non-2xx responses, RequestException covers
            # transport failures, ValueError covers add_movie's own
            # ``tmdb_id <= 0`` guard. A bare ``except Exception`` would
            # silently swallow programming bugs.
            logger.exception("Failed to add movie")
            return JSONResponse({"ok": False, "error": "Failed to add to Radarr"}, status_code=502)
        _record_dn(
            conn,
            email=notify_email,
            title=body.title,
            media_type="movie",
            tmdb_id=body.tmdb_id,
            service="radarr",
        )
        conn.commit()
        return JSONResponse({"ok": True, "message": f"Added '{body.title}' to Radarr"})

    sonarr = build_sonarr_from_db(conn, secret_key)
    if not sonarr:
        return JSONResponse({"ok": False, "error": "Sonarr not configured"}, status_code=503)
    try:
        lookup = sonarr.lookup_series_by_tmdb(body.tmdb_id)
    except (SafeHTTPError, _requests.RequestException):
        logger.exception("Sonarr lookup failed")
        return JSONResponse({"ok": False, "error": "Sonarr lookup failed"}, status_code=502)
    if not lookup:
        return JSONResponse(
            {"ok": False, "error": "Series not found in Sonarr lookup"}, status_code=404
        )
    tvdb_id_raw = lookup.get("tvdbId")
    if not isinstance(tvdb_id_raw, int) or tvdb_id_raw <= 0:
        return JSONResponse({"ok": False, "error": "No TVDB ID for this series"}, status_code=422)
    tvdb_id: int = tvdb_id_raw

    try:
        existing = sonarr.get_series()
    except (SafeHTTPError, _requests.RequestException):
        logger.warning(
            "Sonarr get_series failed during duplicate check — skipping check", exc_info=True
        )
        existing = []
    if any(s.get("tvdbId") == tvdb_id for s in existing):
        return JSONResponse(
            {
                "ok": False,
                "error": f"'{body.title}' is already tracked by Sonarr — manage it from the Library page or Sonarr directly",
            },
            status_code=409,
        )

    if body.search_seasons is not None and len(body.search_seasons) == 0:
        return JSONResponse({"ok": False, "error": "Pick at least one season"}, status_code=400)

    try:
        if body.monitored_seasons is None:
            sonarr.add_series(tvdb_id, body.title)
        else:
            sonarr.add_series_with_seasons(
                tvdb_id,
                body.title,
                body.monitored_seasons,
                body.search_seasons or [],
            )
    except (SafeHTTPError, _requests.RequestException, ValueError):
        logger.exception("Failed to add series")
        return JSONResponse({"ok": False, "error": "Failed to add to Sonarr"}, status_code=502)
    _record_dn(
        conn,
        email=notify_email,
        title=body.title,
        media_type="tv",
        tmdb_id=body.tmdb_id,
        tvdb_id=tvdb_id,
        service="sonarr",
    )
    conn.commit()
    return JSONResponse({"ok": True, "message": f"Added '{body.title}' to Sonarr"})
