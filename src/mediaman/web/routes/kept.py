"""Protected items page and API endpoints.

Handles items shielded from deletion: those marked protected_forever or
snoozed with a future execute_at. Provides page render, JSON listing,
and unprotect action.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from mediaman.audit import log_audit
from mediaman.core.format import days_ago, media_type_badge
from mediaman.core.format import format_bytes as _format_bytes
from mediaman.core.time import now_iso, now_utc
from mediaman.db import get_db
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import format_expiry, resolve_keep_decision
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.models import ACTION_PROTECTED_FOREVER, VALID_KEEP_DURATIONS
from mediaman.web.repository.kept import (
    delete_kept_show,
    delete_protection,
    fetch_active_protection,
    fetch_existing_actions_for_seasons,
    fetch_owned_season_ids,
    fetch_protected_items,
    fetch_seasons_for_show,
    fetch_show_keep_row,
    fetch_show_kept_status,
    fetch_show_title,
    fetch_unkeyed_media_ids,
    set_protected_state,
    show_rating_key_exists,
    upsert_kept_show,
)
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes._helpers import is_admin as _is_admin

# Canonical list of TV / anime season media_type values, shared with the
# library type filter. Keeping the list co-located with
# mediaman.web.repository.library_query would create a circular import;
# instead we redeclare the same tuple here and rely on the unit test to
# detect drift.
_TV_SEASON_TYPES: tuple[str, ...] = ("tv_season", "tv", "season")
_ANIME_SEASON_TYPES: tuple[str, ...] = ("anime_season", "anime")
_ALL_SEASON_TYPES: tuple[str, ...] = _TV_SEASON_TYPES + _ANIME_SEASON_TYPES


def _resolve_show_rating_key(
    conn: sqlite3.Connection, supplied_key: str
) -> tuple[str | None, str | None]:
    """Return (resolved_key, error) for a keep-show request.

    IDOR risk closed by this helper: the previous implementation fell
    back to matching seasons by show_title whenever the supplied
    rating key was missing on the stored rows. Two distinct shows
    sharing a title (a common case -- remakes, international versions,
    generic one-word titles) collided in that branch so user A keeping
    Kingdom would also match user B's Kingdom rows.

    Resolution rules:
      (a) supplied_key is present and at least one media_items row
          carries that exact show_rating_key -- use the supplied key.
      (b) anything else -- return (None, error_message) so the caller
          can 409.

    supplied_key is the raw path parameter. Callers pass it through
    unchanged -- never synthesised from show_title.
    """
    key = (supplied_key or "").strip()
    if key:
        if show_rating_key_exists(conn, key):
            return key, None
        return None, "Unknown show_rating_key"
    return None, "show_rating_key required"


class _KeepShowBody(BaseModel):
    """Body shape for POST /api/show/{show_rating_key}/keep."""

    duration: str = "forever"
    season_ids: list[str] = Field(default_factory=list, max_length=50)


_UNPROTECT_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)

_REMOVE_SHOW_KEEP_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)


logger = logging.getLogger(__name__)

router = APIRouter()


def _fetch_protected(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """Return (forever_items, snoozed_items) from scheduled_actions joined with media_items."""
    now = now_iso()
    rows = fetch_protected_items(conn, now)

    forever = []
    snoozed = []
    for r in rows:
        media_type = r.media_type
        badge_class, type_label = media_type_badge(media_type)
        if media_type in ("tv", "anime") and r.season_number:
            type_label = f"{type_label} . S{r.season_number}"

        item = {
            "sa_id": r.sa_id,
            "media_item_id": r.media_item_id,
            "title": r.title,
            "plex_rating_key": r.plex_rating_key,
            "badge_class": badge_class,
            "type_label": type_label,
            "action": r.action,
            "expiry": format_expiry(r.action, r.execute_at),
            "snooze_duration": r.snooze_duration,
            "file_size": _format_bytes(r.file_size_bytes),
        }

        if r.action == ACTION_PROTECTED_FOREVER:
            forever.append(item)
        else:
            snoozed.append(item)

    return forever, snoozed


@router.get("/kept")
def redirect_kept_to_library(request: Request) -> RedirectResponse:
    """Redirect /kept to the library Kept filter -- auth-gated."""
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/library?type=kept", status_code=301)


@router.get("/kept/page")
def redirect_kept_page(request: Request) -> RedirectResponse:
    """Redirect legacy /kept/page to /library?type=kept -- auth-gated."""
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/library?type=kept", status_code=301)


@router.get("/api/kept")
def api_protected(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all actively kept items as JSON."""
    conn = get_db()
    forever, snoozed = _fetch_protected(conn)
    return JSONResponse({"forever": forever, "snoozed": snoozed})


@router.post("/api/media/{media_item_id}/unprotect")
def api_unprotect(media_item_id: str, username: str = Depends(get_current_admin)) -> JSONResponse:
    """Remove protection from a media item."""
    if not _UNPROTECT_LIMITER.check(username):
        logger.warning("media.unprotect_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many unprotect operations -- slow down"
        )

    conn = get_db()
    action_id = fetch_active_protection(conn, media_item_id)

    if action_id is None:
        return respond_err("not_found", status=404, message="No active protection found")

    delete_protection(conn, action_id)
    log_audit(
        conn,
        media_item_id,
        "unprotected",
        f"Protection removed by {username}",
        actor=username,
    )
    conn.commit()

    logger.info("Unprotected media_item_id=%s by %s", media_item_id, username)
    return respond_ok()


@router.get("/api/show/{show_rating_key}/seasons")
def api_show_seasons(
    show_rating_key: str, request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    """Return all seasons of a show for the keep dialog season picker."""
    conn = get_db()
    season_rows = fetch_seasons_for_show(conn, show_rating_key)

    show_title = ""
    if season_rows:
        show_title = fetch_show_title(conn, show_rating_key) or ""

    seasons = []
    for r in season_rows:
        lw = r.last_watched_at
        last_watched = days_ago(lw) or None

        seasons.append(
            {
                "id": r.id,
                "season_number": r.season_number,
                "title": r.title,
                "kept": r.kept,
                "file_size": _format_bytes(r.file_size_bytes),
                "file_size_bytes": r.file_size_bytes,
                "last_watched": last_watched,
            }
        )

    show_kept_row = fetch_show_kept_status(conn, show_rating_key)

    return JSONResponse(
        {
            "show_title": show_title,
            "show_rating_key": show_rating_key,
            "show_kept": {
                "action": show_kept_row.action,
                "execute_at": show_kept_row.execute_at,
            }
            if show_kept_row
            else None,
            "seasons": seasons,
        }
    )


@router.post("/api/show/{show_rating_key}/keep")
def api_keep_show(
    show_rating_key: str,
    body: _KeepShowBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Keep an entire show (all listed seasons + future seasons via kept_shows rule)."""
    conn = get_db()
    duration = body.duration
    season_ids = body.season_ids

    if not season_ids:
        return respond_err("no_seasons", status=400, message="No seasons selected")

    if duration not in VALID_KEEP_DURATIONS:
        return respond_err("invalid_duration", status=400)

    resolved_key, err = _resolve_show_rating_key(conn, show_rating_key)
    if err or not resolved_key:
        logger.warning(
            "keep_show.rating_key_unresolved supplied=%r user=%s err=%s",
            show_rating_key,
            admin,
            err,
        )
        return respond_err(err or "unknown_show", status=409)

    owned_ids = fetch_owned_season_ids(conn, season_ids, resolved_key)
    if owned_ids != set(season_ids):
        missing = set(season_ids) - owned_ids
        if missing:
            unkeyed_ids = fetch_unkeyed_media_ids(conn, missing)
            if unkeyed_ids:
                logger.warning(
                    "keep_show.fallback_would_have_triggered user=%s "
                    "show_rating_key=%s unkeyed_ids=%s",
                    admin,
                    resolved_key,
                    unkeyed_ids,
                )
        return respond_err(
            "seasons_not_owned", status=400, message="Seasons do not belong to this show"
        )

    now = now_utc()
    decision = resolve_keep_decision(duration, days=VALID_KEEP_DURATIONS.get(duration), now=now)
    action = decision.action
    execute_at = decision.execute_at

    show_title = fetch_show_title(conn, resolved_key) or "Unknown"

    upsert_kept_show(
        conn,
        show_rating_key=resolved_key,
        show_title=show_title,
        action=action,
        execute_at=execute_at,
        snooze_duration=duration,
        created_at=now.isoformat(),
    )

    existing_by_season = fetch_existing_actions_for_seasons(conn, season_ids)

    to_update = [
        (action, execute_at, now.isoformat(), duration, existing_by_season[sid])
        for sid in season_ids
        if sid in existing_by_season
    ]
    to_insert = [
        (
            sid,
            action,
            now.isoformat(),
            execute_at,
            secrets.token_urlsafe(32),
            now.isoformat(),
            duration,
        )
        for sid in season_ids
        if sid not in existing_by_season
    ]

    set_protected_state(conn, to_update=to_update, to_insert=to_insert)

    log_audit(
        conn,
        resolved_key,
        "kept_show",
        f"Show '{show_title}' kept ({duration}) by {admin}",
        actor=admin,
    )
    conn.commit()

    logger.info("Kept show %s (%s) -- %s by %s", resolved_key, show_title, duration, admin)
    return respond_ok()


@router.post("/api/show/{show_rating_key}/remove")
def api_remove_show_keep(
    show_rating_key: str, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    """Remove a show-level keep rule. Individual season keeps are not affected."""
    if not _REMOVE_SHOW_KEEP_LIMITER.check(admin):
        return respond_err(
            "too_many_requests",
            status=429,
            message="Too many remove-show-keep requests; try again shortly.",
        )
    conn = get_db()
    keep_row = fetch_show_keep_row(conn, show_rating_key)

    if keep_row is None:
        return respond_err("not_found", status=404, message="No show-level keep found")

    kept_id, show_title = keep_row
    delete_kept_show(conn, kept_id)
    log_audit(
        conn,
        show_rating_key,
        "removed_show_keep",
        f"Show keep removed for '{show_title}' by {admin}",
        actor=admin,
    )
    conn.commit()

    logger.info("Removed show keep for %s by %s", show_rating_key, admin)
    return respond_ok()


@router.get("/protected")
def redirect_protected_page(request: Request) -> RedirectResponse:
    """Redirect old /protected URL to library kept filter -- auth-gated."""
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/library?type=kept", status_code=301)
