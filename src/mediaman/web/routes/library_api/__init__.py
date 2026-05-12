"""Library JSON API endpoints — package root.

Package layout:

* :mod:`~mediaman.web.repository.delete_intents` — delete-intent
  durability helpers (record / complete / fail / reconcile). Imported
  from the repository layer (§2.7.1) and re-exported here for
  backwards-compatible test patch targets.
* :mod:`~mediaman.web.routes.library_api.redownload` — redownload route,
  request schema, lookup matching, audit-ID generation, and the
  ``_REDOWNLOAD_LIMITER`` singleton. Its sub-router is mounted onto the
  package router below.
* This module (``__init__``) — keep / delete / library-list routes plus
  the shared rate-limiter singletons. Names that tests patch
  (``build_radarr_from_db``, ``build_sonarr_from_db``, the
  ``*_LIMITER`` singletons) are re-exported here so the historic patch
  targets keep working.

Handles all ``/api/library`` and ``/api/media/…`` JSON routes:

* GET  /api/library                — paginated library list
* POST /api/media/{id}/keep        — protect a media item
* POST /api/media/{id}/delete      — delete via Radarr/Sonarr (two-phase)
* POST /api/media/redownload       — trigger a re-download via Radarr/Sonarr

The browser-facing GET /library page lives in the sibling module
:mod:`mediaman.web.routes.library`.
"""

from __future__ import annotations

import logging
import secrets

import requests
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.infra.http import SafeHTTPError
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import resolve_keep_decision
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.models import VALID_KEEP_DURATIONS
from mediaman.web.repository.delete_intents import (
    _complete_delete_intent,
    _fail_delete_intent,
    _record_delete_intent,
    reconcile_pending_delete_intents,
)
from mediaman.web.repository.library_api import (
    NotFound,
    apply_keep_in_tx,
    finalise_delete_in_tx,
    snapshot_media_for_delete,
)
from mediaman.web.repository.library_query import (
    _VALID_SORTS,
    _VALID_TYPES,
    fetch_library,
)
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.library_api.redownload import (
    _REDOWNLOAD_LIMITER,
    _REDOWNLOAD_TITLE_SIMILARITY,
    _pick_lookup_match,
    _redownload_audit_id,
    _RedownloadRequest,
    api_media_redownload,
)
from mediaman.web.routes.library_api.redownload import router as _redownload_router

logger = logging.getLogger(__name__)

router = APIRouter()
router.include_router(_redownload_router)


# ---------------------------------------------------------------------------
# Rate limiters
# ---------------------------------------------------------------------------

# Per-admin cap on keep/unkeep toggles.  These are cheap DB writes with no
# downstream Arr round-trip, so a generous burst cap is fine: 60 per minute
# / 500 per day per actor.
_KEEP_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)

# Per-admin cap on delete triggers.  Each call initiates an Arr delete which
# may trigger a rename/move on disk; tighter than keep: 20 per minute /
# 300 per day per actor.
_DELETE_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=300,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/library")
def api_library(
    q: str = "",
    type: str = "",
    sort: str = "added_desc",
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return paginated library items as JSON."""
    conn = get_db()
    sort = sort if sort in _VALID_SORTS else "added_desc"
    media_type = type if type in _VALID_TYPES else ""

    items, total = fetch_library(
        conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page
    )
    total_pages = max(1, (total + per_page - 1) // per_page)

    return JSONResponse(
        {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }
    )


@router.post("/api/media/{media_id}/keep")
def api_media_keep(
    media_id: str,
    duration: str = Form(...),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Apply protection to a media item."""
    if not _KEEP_LIMITER.check(username):
        logger.warning("media.keep_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many keep operations — slow down"
        )

    conn = get_db()

    if duration not in VALID_KEEP_DURATIONS:
        return respond_err("invalid_duration", status=400)

    now = now_utc()
    decision = resolve_keep_decision(duration, days=VALID_KEEP_DURATIONS[duration], now=now)
    snooze_label = "forever" if duration == "forever" else duration

    # rationale: the helper takes the media-existence check, scheduled_actions
    # UPSERT, and audit row inside one ``BEGIN IMMEDIATE`` block so the SELECT
    # cannot race with a concurrent delete (TOCTOU closed at §9.7).
    try:
        apply_keep_in_tx(
            conn,
            media_id=media_id,
            action=decision.action,
            execute_at=decision.execute_at,
            now_iso=now.isoformat(),
            snooze_label=snooze_label,
            new_token=secrets.token_urlsafe(32),
            audit_detail=f"Kept for {snooze_label} by admin ({username})",
            actor=username,
        )
    except NotFound:
        return respond_err("not_found", status=404)

    logger.info("Media item %s protected for %s by %s", media_id, snooze_label, username)

    return respond_ok({"id": media_id, "duration": snooze_label})


# rationale: three-transaction layout (snapshot → Arr API call → completion)
# must stay together so the delete-intent durability log is opened, updated,
# and closed in the same code path — splitting would require passing the intent
# ID across call boundaries and risk leaving orphaned intent rows on failure.
@router.post("/api/media/{media_id}/delete")
def api_media_delete(
    media_id: str,
    request: Request,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Delete a media item via Radarr/Sonarr.

    Two-phase, three-transaction layout — intentionally split:

    1. **Snapshot transaction** (``BEGIN IMMEDIATE`` … ``COMMIT``)
       — read the media row, capture identifiers, release the lock.
    2. **External Arr call** (no DB transaction held)
       — Radarr / Sonarr round-trip can take seconds; holding a SQLite
       write lock that long would block every other writer in the process.
       A delete-intent row is persisted *before* this step so a crash
       between the Arr call returning and the DB cleanup landing can be
       reconciled by :func:`reconcile_pending_delete_intents` at startup.
    3. **Cleanup transaction** (``BEGIN IMMEDIATE`` … ``COMMIT``)
       — write the audit row, prune ``scheduled_actions``, drop the
       ``media_items`` row, then mark the delete-intent complete.
    """
    if not _DELETE_LIMITER.check(username):
        logger.warning("media.delete_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many delete operations — slow down"
        )
    conn = get_db()

    # Snapshot transaction: the helper opens ``BEGIN IMMEDIATE`` and rolls
    # back on :exc:`NotFound`. We map "absent media row" to 403 so the
    # endpoint cannot be used as an existence oracle (an authenticated
    # caller learns "forbidden" rather than "not found" for unknown ids).
    try:
        snapshot = snapshot_media_for_delete(conn, media_id)
    except NotFound:
        return respond_err("forbidden", status=403)

    title = snapshot.title
    config = request.app.state.config
    is_movie = snapshot.media_type == "movie"

    def _is_already_gone(exc: Exception) -> bool:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        return status == 404

    # Tracks the row id of the delete-intent persisted before any external
    # call.  None means there is no intent to finalise (the no-Arr-id path
    # skips writing one).
    intent_id: int | None = None

    if is_movie:
        client = build_radarr_from_db(conn, config.secret_key)
        if client:
            radarr_id = snapshot.radarr_id
            if radarr_id:
                intent_id = _record_delete_intent(conn, media_id, "radarr", str(radarr_id))
                try:
                    client.delete_movie(radarr_id)
                    logger.info(
                        "Deleted '%s' via Radarr (id %s, with files + exclusion)", title, radarr_id
                    )
                except (SafeHTTPError, requests.RequestException, ArrError, ValueError) as exc:
                    if _is_already_gone(exc):
                        logger.info(
                            "Radarr reports id %s already gone for '%s' — idempotent delete",
                            radarr_id,
                            title,
                        )
                    else:
                        _fail_delete_intent(conn, intent_id, str(exc))
                        logger.exception("Radarr delete failed for '%s'", title)
                        return respond_err(
                            "upstream_delete_failed",
                            status=502,
                            message="Upstream Radarr delete failed — DB row preserved",
                        )
            else:
                logger.info("No stored radarr_id for '%s' — skipping Radarr-level delete.", title)
    else:
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client:
            sid = snapshot.sonarr_id
            season_num = snapshot.season_number
            if sid and season_num is not None:
                intent_id = _record_delete_intent(conn, media_id, "sonarr", str(sid))
                try:
                    sonarr_client.delete_episode_files(sid, season_num)
                    sonarr_client.unmonitor_season(sid, season_num)
                    logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
                    if not sonarr_client.has_remaining_files(sid):
                        sonarr_client.delete_series(sid)
                        logger.info(
                            "No files remain for '%s' — deleted series from Sonarr with exclusion",
                            title,
                        )
                except (SafeHTTPError, requests.RequestException, ArrError, ValueError) as exc:
                    if _is_already_gone(exc):
                        logger.info(
                            "Sonarr reports id %s already gone for '%s' — idempotent delete",
                            sid,
                            title,
                        )
                    else:
                        _fail_delete_intent(conn, intent_id, str(exc))
                        logger.exception("Sonarr delete failed for '%s'", title)
                        return respond_err(
                            "upstream_delete_failed",
                            status=502,
                            message="Upstream Sonarr delete failed — DB row preserved",
                        )

    rk = snapshot.plex_rating_key or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    # Cleanup transaction: audit + scheduled_actions prune + row drop in
    # one ``BEGIN IMMEDIATE`` so a SQLite failure rolls the whole lot
    # back together (M27 fail-closed contract).
    finalise_delete_in_tx(
        conn,
        media_id=media_id,
        audit_detail=detail,
        space_bytes=snapshot.file_size_bytes,
        actor=username,
    )

    if intent_id is not None:
        _complete_delete_intent(conn, intent_id)
    logger.info("Deleted %s (%s) — %s by %s", media_id, title, snapshot.file_path, username)
    return respond_ok({"id": media_id})


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    "_DELETE_LIMITER",
    "_KEEP_LIMITER",
    "_REDOWNLOAD_LIMITER",
    "_REDOWNLOAD_TITLE_SIMILARITY",
    "_RedownloadRequest",
    "_complete_delete_intent",
    "_fail_delete_intent",
    "_pick_lookup_match",
    "_record_delete_intent",
    "_redownload_audit_id",
    "api_library",
    "api_media_delete",
    "api_media_keep",
    "api_media_redownload",
    "build_radarr_from_db",
    "build_sonarr_from_db",
    "reconcile_pending_delete_intents",
    "router",
]
