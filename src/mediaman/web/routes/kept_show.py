"""Show-level keep machinery: keep / remove a whole show's seasons.

Split out of :mod:`mediaman.web.routes.kept` to keep that module under
the size ceiling. Owns the two show-keep routes
(``POST /api/show/{key}/keep`` and ``POST /api/show/{key}/remove``), the
request body schema, the rating-key resolution that closes the IDOR risk,
the season-ownership check, and the season action-row builder. The
``kept`` package mounts this module's ``router`` and re-exports
``_resolve_show_rating_key`` so the historic test import target keeps
working.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import resolve_keep_decision
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.models import VALID_KEEP_DURATIONS
from mediaman.web.repository.kept import (
    delete_kept_show,
    fetch_existing_actions_for_seasons,
    fetch_owned_season_ids,
    fetch_show_keep_row,
    fetch_show_title,
    fetch_unkeyed_media_ids,
    set_protected_state,
    show_rating_key_exists,
    upsert_kept_show,
)
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

router = APIRouter()

_REMOVE_SHOW_KEEP_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)


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


def _validate_keep_show_input(
    conn: sqlite3.Connection,
    supplied_key: str,
    duration: str,
    season_ids: list[str],
    admin: str,
) -> tuple[str | None, JSONResponse | None]:
    """Validate the keep-show request body and rating key.

    Returns ``(resolved_key, None)`` on success or
    ``(None, error_response)`` for the empty-season-list,
    invalid-duration, and unresolved-rating-key branches.
    """
    if not season_ids:
        return None, respond_err("no_seasons", status=400, message="No seasons selected")
    if duration not in VALID_KEEP_DURATIONS:
        return None, respond_err("invalid_duration", status=400)
    resolved_key, err = _resolve_show_rating_key(conn, supplied_key)
    if err or not resolved_key:
        logger.warning(
            "keep_show.rating_key_unresolved supplied=%r user=%s err=%s",
            supplied_key,
            admin,
            err,
        )
        return None, respond_err(err or "unknown_show", status=409)
    return resolved_key, None


def _check_season_ownership(
    conn: sqlite3.Connection,
    season_ids: list[str],
    resolved_key: str,
    admin: str,
) -> JSONResponse | None:
    """Confirm every season in *season_ids* belongs to *resolved_key*.

    Returns ``None`` on success or a 400 envelope when any season is
    unowned. Logs the IDOR-fallback-would-have-triggered warning when
    the unowned IDs lack any show_rating_key — that branch is the
    diagnostic carry-over from the previous fallback behaviour (seasons with no
    show_rating_key were once silently accepted; logging here flags regressions).
    """
    owned_ids = fetch_owned_season_ids(conn, season_ids, resolved_key)
    if owned_ids == set(season_ids):
        return None
    missing = set(season_ids) - owned_ids
    if missing:
        unkeyed_ids = fetch_unkeyed_media_ids(conn, missing)
        if unkeyed_ids:
            logger.warning(
                "keep_show.fallback_would_have_triggered user=%s show_rating_key=%s unkeyed_ids=%s",
                admin,
                resolved_key,
                unkeyed_ids,
            )
    return respond_err(
        "seasons_not_owned", status=400, message="Seasons do not belong to this show"
    )


def _build_season_action_rows(
    season_ids: list[str],
    existing_by_season: dict[str, int],
    *,
    action: str,
    execute_at: str | None,
    duration: str,
    now_iso_str: str,
) -> tuple[
    list[tuple[str, str | None, str, str, int]],
    list[tuple[str, str, str, str | None, str, str, str]],
]:
    """Partition season IDs into update / insert tuples for ``scheduled_actions``.

    Returns ``(to_update, to_insert)`` shaped exactly as
    :func:`set_protected_state` expects.
    """
    to_update = [
        (action, execute_at, now_iso_str, duration, existing_by_season[sid])
        for sid in season_ids
        if sid in existing_by_season
    ]
    to_insert = [
        (
            sid,
            action,
            now_iso_str,
            execute_at,
            secrets.token_urlsafe(32),
            now_iso_str,
            duration,
        )
        for sid in season_ids
        if sid not in existing_by_season
    ]
    return to_update, to_insert


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

    resolved_key, err_response = _validate_keep_show_input(
        conn, show_rating_key, duration, season_ids, admin
    )
    if err_response is not None or resolved_key is None:
        return err_response or respond_err("unknown_show", status=409)

    ownership_err = _check_season_ownership(conn, season_ids, resolved_key, admin)
    if ownership_err is not None:
        return ownership_err

    now = now_utc()
    decision = resolve_keep_decision(duration, days=VALID_KEEP_DURATIONS.get(duration), now=now)
    show_title = fetch_show_title(conn, resolved_key) or "Unknown"
    now_iso_str = now.isoformat()

    upsert_kept_show(
        conn,
        show_rating_key=resolved_key,
        show_title=show_title,
        action=decision.action,
        execute_at=decision.execute_at,
        snooze_duration=duration,
        created_at=now_iso_str,
    )

    existing_by_season = fetch_existing_actions_for_seasons(conn, season_ids)
    to_update, to_insert = _build_season_action_rows(
        season_ids,
        existing_by_season,
        action=decision.action,
        execute_at=decision.execute_at,
        duration=duration,
        now_iso_str=now_iso_str,
    )

    # The seasons write and the audit row share one transaction so a failure
    # between them rolls both back together. Helper extraction must preserve
    # this coupling.
    with conn:
        set_protected_state(conn, to_update=to_update, to_insert=to_insert)
        log_audit(
            conn,
            resolved_key,
            "kept_show",
            f"Show '{show_title}' kept ({duration}) by {admin}",
            actor=admin,
        )

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
    with conn:
        delete_kept_show(conn, kept_id)
        log_audit(
            conn,
            show_rating_key,
            "removed_show_keep",
            f"Show keep removed for '{show_title}' by {admin}",
            actor=admin,
        )

    logger.info("Removed show keep for %s by %s", show_rating_key, admin)
    return respond_ok()
