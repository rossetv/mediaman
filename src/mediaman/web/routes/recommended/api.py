from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from mediaman.core.audit import log_audit
from mediaman.core.time import now_iso
from mediaman.crypto import generate_download_token
from mediaman.db import get_db
from mediaman.services import watch_catalog
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra import SafeHTTPError, get_string_setting
from mediaman.services.openai.recommendations.repository import (
    SuggestionDetail,
    fetch_suggestion_by_id,
    fetch_suggestion_header,
    mark_downloaded,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_admin, get_current_user
from mediaman.web.auth.password_hash import get_user_email
from mediaman.web.repository.recommended import fetch_recommendations_page
from mediaman.web.responses import respond_failure

logger = logging.getLogger(__name__)

router = APIRouter()

_DOWNLOAD_ACTION_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=500)
_SHARE_TOKEN_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=500)

# TTL for share tokens minted by the share-token endpoint.  Named with the
# unit; computed via timedelta to avoid the manual-seconds footgun.
_SHARE_TOKEN_TTL_DAYS = 14


def reset_download_action_limiter() -> None:
    _DOWNLOAD_ACTION_LIMITER.reset()


def reset_share_token_limiter() -> None:
    _SHARE_TOKEN_LIMITER.reset()


_RECOMMENDED_LIMIT_DEFAULT = 50
_RECOMMENDED_LIMIT_MAX = 200


@router.get("/api/recommended")
def api_recommended(
    admin: str = Depends(get_current_user),
    limit: int = Query(default=_RECOMMENDED_LIMIT_DEFAULT, ge=1, le=_RECOMMENDED_LIMIT_MAX),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> JSONResponse:
    conn = get_db()
    sliced, total = fetch_recommendations_page(conn, limit=limit, offset=offset)
    return JSONResponse(
        {
            "recommendations": sliced,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.post("/api/recommended/{recommendation_id}/share-token")
def api_share_token(
    recommendation_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    if not _SHARE_TOKEN_LIMITER.check(admin):
        return respond_failure("Too many requests", status=429)

    conn = get_db()
    config = request.app.state.config

    row = fetch_suggestion_header(conn, recommendation_id)
    if not row:
        return respond_failure("Recommendation not found", status=404)

    if not row.tmdb_id:
        return respond_failure(
            "Cannot generate share link -- no TMDB identifier for this recommendation", status=422
        )

    base_url = (get_string_setting(conn, "base_url") or "").rstrip("/")
    if not base_url:
        return respond_failure("Base URL not configured -- cannot generate share link", status=503)

    expires_at = (datetime.now(UTC) + timedelta(days=_SHARE_TOKEN_TTL_DAYS)).isoformat()

    # No email: the payload is only base64, and whoever is given the link can
    # read it. The redeemer then records no "ready" notification for it.
    share_token = generate_download_token(
        email=None,
        action="download",
        title=row.title,
        media_type=row.media_type,
        tmdb_id=row.tmdb_id,
        recommendation_id=row.id,
        secret_key=config.secret_key,
        ttl_days=_SHARE_TOKEN_TTL_DAYS,
    )
    share_url = f"{base_url}/download/{share_token}"

    logger.info(
        "Share token minted by admin '%s' for recommendation_id=%d title='%s'",
        _sanitise_log_field(admin),
        recommendation_id,
        row.title,
    )
    return JSONResponse(
        {"ok": True, "token": share_token, "share_url": share_url, "expires_at": expires_at}
    )


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitise_title(title: str | None) -> str:
    if not title:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", title).strip()
    return cleaned[:256]


def _add_rec_to_radarr(
    conn: sqlite3.Connection,
    *,
    notify_email: str | None,
    actor: str,
    row: SuggestionDetail,
    recommendation_id: int,
    secret_key: str,
) -> JSONResponse:
    """Add a movie recommendation to Radarr, record the notification and return the response.
    notify_email is None when the issuing admin has no email set: the movie is still added
    but no notification row is recorded.
    """
    client = build_radarr_from_db(conn, secret_key)
    if not client:
        return respond_failure("Radarr not configured", status=200)
    tmdb_id = row.tmdb_id
    if not tmdb_id:
        return respond_failure("No TMDB ID -- cannot add to Radarr", status=200)
    safe_title = _sanitise_title(row.title)
    client.add_movie(tmdb_id, safe_title)
    logger.info("Added movie '%s' (tmdb:%s) to Radarr", safe_title, tmdb_id)
    with conn:
        mark_downloaded(conn, recommendation_id, now_iso())
        log_audit(
            conn,
            safe_title,
            "downloaded",
            f"Added '{safe_title}' to Radarr by {actor}",
            actor=actor,
        )
        if notify_email:
            record_download_notification(
                conn,
                email=notify_email,
                title=safe_title,
                media_type="movie",
                tmdb_id=tmdb_id,
                service="radarr",
            )
    return JSONResponse({"ok": True, "message": f"Added '{safe_title}' to Radarr"})


def _add_rec_to_sonarr(
    conn: sqlite3.Connection,
    *,
    notify_email: str | None,
    actor: str,
    row: SuggestionDetail,
    recommendation_id: int,
    secret_key: str,
) -> JSONResponse:
    """Add a TV recommendation to Sonarr, record the notification and return the response.
    notify_email is None when the issuing admin has no email set: the show is still added
    but no notification row is recorded.
    """
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return respond_failure("Sonarr not configured", status=200)
    tmdb_id = row.tmdb_id
    if not tmdb_id:
        return respond_failure("No TMDB ID -- cannot add to Sonarr", status=200)
    safe_title = _sanitise_title(row.title)
    results = client.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")
    if not results:
        return respond_failure("Show not found in Sonarr lookup", status=200)
    # Filter for the entry whose tmdbId matches the requested id — Sonarr may
    # return results in a different order on different instances.
    matched = next(
        (r for r in results if str(r.get("tmdbId", "")).strip() == str(tmdb_id).strip()),
        None,
    )
    if matched is None:
        return respond_failure("No matching TMDB entry in Sonarr lookup", status=200)
    raw_tvdb_id = matched.get("tvdbId")
    if not raw_tvdb_id:
        return respond_failure("No TVDB ID found for this show", status=200)
    # Sonarr's lookup body is an untyped dict; ``tvdbId`` may arrive as a
    # string. Coerce before it reaches ``add_series`` (and the %d-free log
    # below) so a non-int value cannot 500 the handler. Mirror the sibling
    # ``_sonarr_add_and_record`` coercion.
    try:
        tvdb_id = int(str(raw_tvdb_id))
    except ValueError:
        return respond_failure("Invalid TVDB ID for this show", status=200)
    if tvdb_id <= 0:
        return respond_failure("Invalid TVDB ID for this show", status=200)
    client.add_series(tvdb_id, safe_title)
    logger.info("Added series '%s' (tvdb:%d) to Sonarr", safe_title, tvdb_id)
    with conn:
        mark_downloaded(conn, recommendation_id, now_iso())
        log_audit(
            conn,
            safe_title,
            "downloaded",
            f"Added '{safe_title}' to Sonarr by {actor}",
            actor=actor,
        )
        if notify_email:
            record_download_notification(
                conn,
                email=notify_email,
                title=safe_title,
                media_type="tv",
                tmdb_id=tmdb_id,
                tvdb_id=tvdb_id,
                service="sonarr",
            )
    return JSONResponse({"ok": True, "message": f"Added '{safe_title}' to Sonarr"})


@router.post("/api/recommended/{recommendation_id}/download")
def api_download_recommendation(
    recommendation_id: int, request: Request, admin: str = Depends(get_current_user)
) -> JSONResponse:
    if not _DOWNLOAD_ACTION_LIMITER.check(admin):
        return respond_failure("Too many requests", status=429)

    conn = get_db()
    config = request.app.state.config

    row = fetch_suggestion_by_id(conn, recommendation_id)
    if not row:
        return respond_failure("Recommendation not found", status=404)

    if not row.tmdb_id:
        return respond_failure("No TMDB ID -- cannot add to Radarr/Sonarr", status=200)

    # ``admin`` is a username; resolve it to the admin's notification email
    # once (``None`` when unset) and hand the resolved value to the helpers.
    notify_email = get_user_email(conn, admin)

    try:
        if row.media_type == "movie":
            return _add_rec_to_radarr(
                conn,
                notify_email=notify_email,
                actor=admin,
                row=row,
                recommendation_id=recommendation_id,
                secret_key=config.secret_key,
            )
        return _add_rec_to_sonarr(
            conn,
            notify_email=notify_email,
            actor=admin,
            row=row,
            recommendation_id=recommendation_id,
            secret_key=config.secret_key,
        )

    except SafeHTTPError as exc:
        if exc.status_code in (409, 422):
            return respond_failure(f"'{row.title}' already exists in your library", status=409)
        logger.exception(
            "Failed to add recommendation '%s': HTTP %s",
            row.title,
            exc.status_code,
        )
        return respond_failure("Failed to add to download queue", status=502)
    except (ArrError, ValueError):
        logger.exception("Failed to add recommendation '%s'", row.title)
        return respond_failure("Failed to add to download queue", status=502)
    finally:
        # Watch holds Radarr and Sonarr state briefly; drop it so the title shows queued.
        watch_catalog.invalidate()
