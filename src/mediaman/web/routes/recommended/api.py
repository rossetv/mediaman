from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

import requests
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse

from mediaman.core.audit import log_audit
from mediaman.core.time import now_iso
from mediaman.crypto import generate_download_token
from mediaman.db import get_db
from mediaman.services import watch_catalog
from mediaman.services.arr.add_outcome import movie_add_landed, series_add_landed
from mediaman.services.arr.base import ArrError, SeasonsNotListed
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import (
    add_or_remonitor_movie,
    is_series_already_tracked,
    radarr_holds_movie,
    sonarr_tracks_series,
)
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra import SafeHTTPError, get_string_setting
from mediaman.services.openai.recommendations import user_picks
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
from mediaman.web.routes.download.confirm import drop_arr_cache
from mediaman.web.routes.search._seasons import (
    SeasonPicks,
    add_to_tracked_show,
    no_season,
    seasons_not_listed,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# The largest whole number SQLite takes.
_RowId = Annotated[int, Path(ge=1, le=2**63 - 1)]

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
    recommendation_id: _RowId,
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


def _already_there(row: SuggestionDetail) -> JSONResponse:
    already = (
        f"'{row.title}' is already in Radarr"
        if row.media_type == "movie"
        else f"'{row.title}' has already been added"
    )
    return respond_failure(already, status=409)


def _add_rec_to_radarr(
    conn: sqlite3.Connection,
    *,
    notify_email: str | None,
    actor: str,
    row: SuggestionDetail,
    mark: Callable[[], None],
    secret_key: str,
    picks: SeasonPicks,
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
    try:
        outcome = add_or_remonitor_movie(client, tmdb_id, safe_title)
    except SafeHTTPError as exc:
        # Only a failed add can mean "already there": Radarr answers 409, 422 or,
        # for a movie it holds, 400, so an add that raced the check above ends
        # as already there and any other 400 stays a failure.
        if exc.status_code in (409, 422) or (
            exc.status_code == 400 and radarr_holds_movie(client, tmdb_id)
        ):
            return _already_there(row)
        if not movie_add_landed(exc, client, tmdb_id):
            raise
        outcome = "added"
    if outcome == "already_tracked":
        return _already_there(row)
    if outcome == "bad_entry":
        return respond_failure("Radarr entry is in an unexpected state", status=502)
    logger.info("Added movie '%s' (tmdb:%s) to Radarr", safe_title, tmdb_id)
    with conn:
        mark()
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
                username=actor,
            )
    return JSONResponse({"ok": True, "message": f"Added '{safe_title}' to Radarr"})


def _add_rec_to_sonarr(
    conn: sqlite3.Connection,
    *,
    notify_email: str | None,
    actor: str,
    row: SuggestionDetail,
    mark: Callable[[], None],
    secret_key: str,
    picks: SeasonPicks,
) -> JSONResponse:
    """Add a TV recommendation, or its seasons *picks* names, to Sonarr, record the notification
    and return the response.
    notify_email is None when the issuing admin has no email set: the show is still added
    but no notification row is recorded.
    """
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return respond_failure("Downloads aren't set up for shows", status=200)
    tmdb_id = row.tmdb_id
    if not tmdb_id:
        return respond_failure("No TMDB ID for this show", status=200)
    safe_title = _sanitise_title(row.title)
    results = client.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")
    if not results:
        return respond_failure("The download service doesn't know this show", status=200)
    # Filter for the entry whose tmdbId matches the requested id — Sonarr may
    # return results in a different order on different instances.
    matched = next(
        (r for r in results if str(r.get("tmdbId", "")).strip() == str(tmdb_id).strip()),
        None,
    )
    if matched is None:
        return respond_failure("No matching TMDB entry for this show", status=200)
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
    tracked = is_series_already_tracked(client, tvdb_id)
    if tracked and not picks.search_seasons:
        return _already_there(row)
    if tracked:
        resp = add_to_tracked_show(
            conn,
            client,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            title=safe_title,
            numbers=picks.search_seasons or (),
            actor=actor,
            email=notify_email,
        )
        if resp.status_code < 400:
            try:
                with conn:
                    mark()
            except sqlite3.Error:
                # The seasons were asked for, whatever is known of the recommendation.
                logger.exception("'%s' could not be marked downloaded", safe_title)
        return resp
    seasons = picks.to_add()
    if seasons is not None and not seasons:
        return no_season()
    try:
        if seasons is None:
            client.add_series(tvdb_id, safe_title)
        else:
            client.add_series_with_seasons(tvdb_id, safe_title, seasons)
    except SeasonsNotListed as exc:
        return seasons_not_listed(exc, seasons or [])
    except SafeHTTPError as exc:
        # Same as Radarr, for a series Sonarr already tracks.
        if exc.status_code in (409, 422) or (
            exc.status_code == 400 and sonarr_tracks_series(client, tvdb_id)
        ):
            return _already_there(row)
        if not series_add_landed(exc, client, tvdb_id):
            raise
    logger.info("Added series '%s' (tvdb:%d) to Sonarr", safe_title, tvdb_id)
    with conn:
        mark()
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
                username=actor,
            )
    return JSONResponse({"ok": True, "message": f"'{safe_title}' is asked for"})


@router.post("/api/recommended/{recommendation_id}/download")
def api_download_recommendation(
    recommendation_id: _RowId,
    request: Request,
    body: SeasonPicks | None = None,
    admin: str = Depends(get_current_user),
) -> JSONResponse:
    if not _DOWNLOAD_ACTION_LIMITER.check(admin):
        return respond_failure("Too many requests", status=429)
    conn = get_db()
    row = fetch_suggestion_by_id(conn, recommendation_id)
    return _download(
        conn,
        request,
        admin,
        row,
        lambda: mark_downloaded(conn, recommendation_id, now_iso()),
        body or SeasonPicks(),
    )


@router.post("/api/watch/picks/{pick_id}/download")
def api_download_pick(
    pick_id: _RowId,
    request: Request,
    body: SeasonPicks | None = None,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """Download one of the person's own picks; anyone else's is not found."""
    if not _DOWNLOAD_ACTION_LIMITER.check(username):
        return respond_failure("Too many requests", status=429)
    conn = get_db()
    row = user_picks.fetch_pick(conn, username, pick_id)
    return _download(
        conn,
        request,
        username,
        row,
        lambda: user_picks.mark_downloaded(conn, username, pick_id, now_iso()),
        body or SeasonPicks(),
    )


def _download(
    conn: sqlite3.Connection,
    request: Request,
    username: str,
    row: SuggestionDetail | None,
    mark: Callable[[], None],
    picks: SeasonPicks,
) -> JSONResponse:
    if not row:
        return respond_failure("Recommendation not found", status=404)

    if not row.tmdb_id:
        return respond_failure("No TMDB ID -- cannot be downloaded", status=200)

    # Resolve the username to the notification email once (``None`` when unset) and hand
    # the resolved value to the helpers.
    notify_email = get_user_email(conn, username)
    add = _add_rec_to_radarr if row.media_type == "movie" else _add_rec_to_sonarr
    try:
        return add(
            conn,
            notify_email=notify_email,
            actor=username,
            row=row,
            mark=mark,
            secret_key=request.app.state.config.secret_key,
            picks=picks,
        )

    except SafeHTTPError as exc:
        logger.exception(
            "Failed to add recommendation '%s': HTTP %s",
            row.title,
            exc.status_code,
        )
        return respond_failure("Failed to add to download queue", status=502)
    except (requests.RequestException, ArrError, ValueError):
        logger.exception("Failed to add recommendation '%s'", row.title)
        return respond_failure("Failed to add to download queue", status=502)
    finally:
        # Watch and the emailed page hold Radarr and Sonarr state briefly; drop it so the
        # title shows queued.
        watch_catalog.invalidate()
        drop_arr_cache()
