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
import sqlite3

import requests
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.infra import SafeHTTPError
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
    MediaDeleteSnapshot,
    NotFound,
    apply_keep_in_tx,
    finalise_delete_in_tx,
    snapshot_media_for_delete,
)
from mediaman.web.repository.library_query import (
    VALID_SORTS,
    VALID_TYPES,
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
    sort = sort if sort in VALID_SORTS else "added_desc"
    media_type = type if type in VALID_TYPES else ""

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


def _is_already_gone(exc: Exception) -> bool:
    """Return True when an Arr exception carries a 404 — already-deleted upstream."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    return status == 404


def _try_radarr_delete(
    client: ArrClient,
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
) -> tuple[int | None, JSONResponse | None]:
    """Delete a movie via Radarr; returns ``(intent_id, short_circuit_resp)``.

    The response is ``None`` when the caller should proceed to the cleanup
    transaction; a non-None ``JSONResponse`` means the handler must return
    immediately (e.g. 502 on an upstream failure).
    """
    title = snapshot.title
    radarr_id = snapshot.radarr_id
    if not radarr_id:
        logger.info("No stored radarr_id for '%s' — skipping Radarr-level delete.", title)
        return None, None
    intent_id = _record_delete_intent(conn, media_id, "radarr", str(radarr_id))
    try:
        client.delete_movie(radarr_id)
        logger.info("Deleted '%s' via Radarr (id %s, with files + exclusion)", title, radarr_id)
    except (SafeHTTPError, requests.RequestException, ArrError, ValueError) as exc:
        if _is_already_gone(exc):
            logger.info(
                "Radarr reports id %s already gone for '%s' — idempotent delete",
                radarr_id,
                title,
            )
            return intent_id, None
        _fail_delete_intent(conn, intent_id, str(exc))
        logger.exception("Radarr delete failed for '%s'", title)
        return intent_id, respond_err(
            "upstream_delete_failed",
            status=502,
            message="Upstream Radarr delete failed — DB row preserved",
        )
    return intent_id, None


def _try_sonarr_delete(
    sonarr_client: ArrClient,
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
) -> tuple[int | None, JSONResponse | None]:
    """Delete a TV season (and the show if empty) via Sonarr.

    Returns ``(intent_id, short_circuit_resp)``: when the response is
    ``None`` the caller proceeds to the cleanup transaction; otherwise
    the handler returns the response immediately.
    """
    title = snapshot.title
    sid = snapshot.sonarr_id
    season_num = snapshot.season_number
    if not (sid and season_num is not None):
        return None, None
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
            return intent_id, None
        _fail_delete_intent(conn, intent_id, str(exc))
        logger.exception("Sonarr delete failed for '%s'", title)
        return intent_id, respond_err(
            "upstream_delete_failed",
            status=502,
            message="Upstream Sonarr delete failed — DB row preserved",
        )
    return intent_id, None


def _run_arr_delete_branch(
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
    secret_key: str,
) -> tuple[int | None, JSONResponse | None]:
    """Dispatch to the Radarr or Sonarr branch based on the snapshot's media type."""
    if snapshot.media_type == "movie":
        client = build_radarr_from_db(conn, secret_key)
        if client is None:
            return None, None
        return _try_radarr_delete(client, snapshot, conn=conn, media_id=media_id)
    sonarr_client = build_sonarr_from_db(conn, secret_key)
    if sonarr_client is None:
        return None, None
    return _try_sonarr_delete(sonarr_client, snapshot, conn=conn, media_id=media_id)


def _finalise_delete(
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
    intent_id: int | None,
    username: str,
) -> None:
    """Cleanup transaction: audit + scheduled_actions prune + row drop.

    Wrapped in ``BEGIN IMMEDIATE`` by :func:`finalise_delete_in_tx` so a
    SQLite failure rolls the whole lot back together (fail-closed: no
    half-deleted row). The pending delete-intent row, if any, is marked
    complete after the cleanup commits.
    """
    title = snapshot.title
    rk = snapshot.plex_rating_key or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
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

    intent_id, short_circuit = _run_arr_delete_branch(
        snapshot,
        conn=conn,
        media_id=media_id,
        secret_key=request.app.state.config.secret_key,
    )
    if short_circuit is not None:
        return short_circuit

    _finalise_delete(snapshot, conn=conn, media_id=media_id, intent_id=intent_id, username=username)
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
