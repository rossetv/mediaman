from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Literal

import requests as _requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.core.audit import log_audit
from mediaman.db import get_db
from mediaman.services import watch_catalog
from mediaman.services.arr import ArrError
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import add_or_remonitor_movie, is_series_already_tracked
from mediaman.services.downloads.notifications import record_download_notification as _record_dn
from mediaman.services.infra import SafeHTTPError
from mediaman.services.rate_limit import ActionRateLimiter, RateLimiter, get_client_ip
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_email as _get_user_email
from mediaman.web.responses import respond_failure

logger = logging.getLogger(__name__)


# Per-admin burst limiter: 20 downloads per minute, 200 per day.
_DOWNLOAD_ADMIN_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=200,
)

# Per-IP limiter: 30 downloads per minute (covers unauthenticated / shared sessions).
_DOWNLOAD_IP_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)

# Per-process dedup window; must be in-process state to survive across requests
# in the same worker — a request-scoped or dependency-injected object would
# reset on every call and never suppress double-clicks.
# Duplicate-request suppression: block identical (username, tmdb_id, media_type, seasons)
# submissions within a short window to prevent accidental double-clicks from adding
# duplicates. The season set is included in the dedup key so a TV submission for
# seasons {1,2} does not block a follow-up submission for season {3} on the same series.
_DOWNLOAD_DEDUP_WINDOW_SECONDS = 10.0
_download_dedup: dict[tuple[str, int, str, str], float] = {}
_download_dedup_lock = threading.Lock()


def _seasons_dedup_token(
    monitored_seasons: list[int] | None,
    search_seasons: list[int] | None,
) -> str:
    """Return a stable token for the season selection. None (all seasons) and an empty list
    stay distinct, so "monitor everything" cannot collide with "monitor only season 1";
    sorting makes it order-stable.
    """
    mon = (
        "*"
        if monitored_seasons is None
        else ",".join(str(season) for season in sorted(monitored_seasons))
    )
    srch = (
        "*"
        if search_seasons is None
        else ",".join(str(season) for season in sorted(search_seasons))
    )
    return f"m={mon};s={srch}"


def _is_duplicate_download(
    username: str,
    tmdb_id: int,
    media_type: str,
    seasons_token: str = "",
) -> bool:
    """Return True if an identical request was submitted within the dedup window; stale entries
    are cleaned on each call to bound memory.
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


def _release_download(
    username: str,
    tmdb_id: int,
    media_type: str,
    seasons_token: str = "",
) -> None:
    """Forget a dedup entry so a request that added nothing can be retried at once."""
    with _download_dedup_lock:
        _download_dedup.pop((username, tmdb_id, media_type, seasons_token), None)


class _DownloadRequest(BaseModel):
    """Body schema for POST /api/search/download. extra="forbid" turns unknown keys into a 422,
    media_type is limited to the two handled values, the title to 256 chars and the season
    lists to 100 entries to refuse flood payloads.
    """

    model_config = ConfigDict(extra="forbid")

    media_type: Literal["movie", "tv"]
    tmdb_id: int = Field(ge=1)
    title: str = Field(max_length=256)
    monitored_seasons: list[int] | None = Field(default=None, max_length=100)
    search_seasons: list[int] | None = Field(default=None, max_length=100)


def _resolve_admin_email(conn: sqlite3.Connection, admin: str) -> str | None:
    """Return the notification email for admin, or None if unset. Without an email the caller
    skips the download_notifications row: a placeholder such as the bare username once sent
    the row into a forever-retry loop in the Mailgun client.
    """
    return _get_user_email(conn, admin)


router = APIRouter()


def _check_rate_limits(admin: str, request: Request) -> JSONResponse | None:
    if not _DOWNLOAD_ADMIN_LIMITER.check(admin):
        logger.warning("search.download_throttled user=%s", _sanitise_log_field(admin))
        return respond_failure("Too many download requests — slow down", status=429)
    client_ip = get_client_ip(request)
    if not _DOWNLOAD_IP_LIMITER.check(client_ip):
        logger.warning("search.download_ip_throttled ip=%s", client_ip)
        return respond_failure("Too many download requests from this IP — slow down", status=429)
    return None


def _dedup_token(body: _DownloadRequest) -> str:
    return _seasons_dedup_token(body.monitored_seasons, body.search_seasons)


def _check_dedup(admin: str, body: _DownloadRequest) -> JSONResponse | None:
    seasons_token = _dedup_token(body)
    if _is_duplicate_download(admin, body.tmdb_id, body.media_type, seasons_token):
        logger.info(
            "search.download_duplicate_suppressed user=%s tmdb=%s type=%s seasons=%s",
            _sanitise_log_field(admin),
            body.tmdb_id,
            body.media_type,
            seasons_token,
        )
        return respond_failure("Duplicate request — wait a moment before retrying", status=429)
    return None


def _submit_movie(
    conn: sqlite3.Connection,
    secret_key: str,
    body: _DownloadRequest,
    notify_email: str | None,
    actor: str,
) -> JSONResponse:
    radarr = build_radarr_from_db(conn, secret_key)
    if not radarr:
        return respond_failure("Radarr not configured", status=503)
    try:
        outcome = add_or_remonitor_movie(radarr, body.tmdb_id, body.title)
        if outcome == "already_tracked":
            return respond_failure(f"'{body.title}' is already in Radarr", status=409)
        if outcome == "bad_entry":
            return respond_failure("Radarr entry is in an unexpected state", status=502)
    except (SafeHTTPError, _requests.RequestException, ValueError, ArrError):
        # Narrow exception list: SafeHTTPError covers Radarr's non-2xx
        # responses, RequestException covers transport failures, ValueError
        # covers add_movie's own ``tmdb_id <= 0`` guard, ArrError covers
        # configuration failures (e.g. no root folder configured). A bare
        # ``except Exception`` would silently swallow programming bugs.
        logger.exception("Failed to add movie")
        return respond_failure("Failed to add to Radarr", status=502)
    if notify_email is not None:
        _record_dn(
            conn,
            email=notify_email,
            title=body.title,
            media_type="movie",
            tmdb_id=body.tmdb_id,
            service="radarr",
        )
    else:
        logger.info(
            "search.download_notification_skipped reason=no_admin_email media=movie tmdb=%s",
            body.tmdb_id,
        )
    log_audit(
        conn, body.title, "downloaded", f"Added '{body.title}' to Radarr by {actor}", actor=actor
    )
    conn.commit()
    return JSONResponse({"ok": True, "message": f"Added '{body.title}' to Radarr"})


def _resolve_tvdb_id(
    sonarr: ArrClient, body: _DownloadRequest
) -> tuple[int | None, JSONResponse | None]:
    try:
        lookup = sonarr.lookup_series_by_tmdb(body.tmdb_id)
    except (SafeHTTPError, _requests.RequestException, ArrError):
        logger.exception("Sonarr lookup failed")
        return None, respond_failure("Sonarr lookup failed", status=502)
    if not lookup:
        return None, respond_failure("Series not found in Sonarr lookup", status=404)
    tvdb_id_raw = lookup.get("tvdbId")
    if not isinstance(tvdb_id_raw, int) or tvdb_id_raw <= 0:
        return None, respond_failure("No TVDB ID for this series", status=422)
    return tvdb_id_raw, None


def _submit_tv(
    conn: sqlite3.Connection,
    secret_key: str,
    body: _DownloadRequest,
    notify_email: str | None,
    actor: str,
) -> JSONResponse:
    sonarr = build_sonarr_from_db(conn, secret_key)
    if not sonarr:
        return respond_failure("Sonarr not configured", status=503)
    tvdb_id, err = _resolve_tvdb_id(sonarr, body)
    if err is not None:
        return err
    assert tvdb_id is not None
    try:
        already_tracked = is_series_already_tracked(sonarr, tvdb_id)
    except (SafeHTTPError, _requests.RequestException, ArrError):
        # Cannot verify duplicate status — returning 503 is safer than
        # proceeding blindly and potentially creating a duplicate in Sonarr.
        logger.warning(
            "Sonarr get_series failed during duplicate check — refusing to proceed",
            exc_info=True,
        )
        return respond_failure(
            "Cannot verify series status — Sonarr unreachable, please retry", status=503
        )
    if already_tracked:
        return respond_failure(
            f"'{body.title}' is already tracked by Sonarr — manage it there",
            status=409,
        )
    if body.search_seasons is not None and not body.search_seasons:
        return respond_failure("Pick at least one season", status=400)
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
    except (SafeHTTPError, _requests.RequestException, ValueError, ArrError):
        logger.exception("Failed to add series")
        return respond_failure("Failed to add to Sonarr", status=502)
    if notify_email is not None:
        _record_dn(
            conn,
            email=notify_email,
            title=body.title,
            media_type="tv",
            tmdb_id=body.tmdb_id,
            tvdb_id=tvdb_id,
            service="sonarr",
        )
    else:
        logger.info(
            "search.download_notification_skipped reason=no_admin_email media=tv tmdb=%s",
            body.tmdb_id,
        )
    log_audit(
        conn, body.title, "downloaded", f"Added '{body.title}' to Sonarr by {actor}", actor=actor
    )
    conn.commit()
    return JSONResponse({"ok": True, "message": f"Added '{body.title}' to Sonarr"})


@router.post("/api/search/download")
def api_download(
    body: _DownloadRequest, request: Request, admin: str = Depends(get_current_user)
) -> JSONResponse:
    rate_err = _check_rate_limits(admin, request)
    if rate_err is not None:
        return rate_err

    # Duplicate-request suppression: same (admin, tmdb_id, media_type, seasons)
    # within the dedup window indicates a double-click or replay.
    dedup_err = _check_dedup(admin, body)
    if dedup_err is not None:
        return dedup_err

    try:
        conn = get_db()
        secret_key = request.app.state.config.secret_key
        notify_email = _resolve_admin_email(conn, admin)
        if body.media_type == "movie":
            resp = _submit_movie(conn, secret_key, body, notify_email, admin)
        else:
            resp = _submit_tv(conn, secret_key, body, notify_email, admin)
    except BaseException:
        _release_download(admin, body.tmdb_id, body.media_type, _dedup_token(body))
        raise
    if resp.status_code >= 400:
        _release_download(admin, body.tmdb_id, body.media_type, _dedup_token(body))
    # A 409 says the Arr holds the title, which the held view may not yet show.
    if resp.status_code < 400 or resp.status_code == 409:
        watch_catalog.invalidate()
    return resp
