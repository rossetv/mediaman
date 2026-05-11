"""Library JSON API endpoints — package root.

Package layout:

* :mod:`.delete_intents` — delete-intent durability helpers (record /
  complete / fail / reconcile).
* :mod:`.redownload` — redownload request schema, lookup matching, the
  per-service handlers, and audit-ID generation.
* This module (``__init__``) — rate-limiter constants, route handlers,
  the per-service delete helpers (which share the in-flight intent-id
  bookkeeping with the orchestrator), and re-exports of the above for
  backwards-compatible imports.

Handles all ``/api/library`` and ``/api/media/…`` JSON routes:

* GET  /api/library                — paginated library list
* POST /api/media/{id}/keep        — protect a media item
* POST /api/media/{id}/delete      — delete via Radarr/Sonarr (two-phase)
* POST /api/media/redownload       — trigger a re-download via Radarr/Sonarr

The browser-facing GET /library page lives in :mod:`.library`.  Tests
patch ``mediaman.web.routes.library_api.build_radarr_from_db`` (and
sister builds) at the package barrel.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse

from mediaman.core.audit import log_audit
from mediaman.db import get_db
from mediaman.web.repository.library_query import (
    _VALID_SORTS,
    _VALID_TYPES,
    fetch_library,
)
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED, VALID_KEEP_DURATIONS
from mediaman.web.responses import respond_err, respond_ok

# Re-exports for backwards-compatible imports
from mediaman.web.repository.delete_intents import (
    _complete_delete_intent,
    _fail_delete_intent,
    _record_delete_intent,
    reconcile_pending_delete_intents,
)
from mediaman.web.repository.delete_intents import (
    finalise_media_delete as _finalise_media_delete,
)
from mediaman.web.repository.delete_intents import (
    handle_radarr_delete as _handle_radarr_delete,
)
from mediaman.web.repository.delete_intents import (
    handle_sonarr_delete as _handle_sonarr_delete,
)
from mediaman.web.repository.delete_intents import (
    snapshot_media_for_delete as _snapshot_media_for_delete,
)
from mediaman.web.routes.library_api.redownload import (
    _REDOWNLOAD_TITLE_SIMILARITY,
    _pick_lookup_match,
    _redownload_audit_id,
    _RedownloadRequest,
)
from mediaman.web.routes.library_api.redownload import (
    try_radarr_redownload as _try_radarr_redownload,
)
from mediaman.web.routes.library_api.redownload import (
    try_sonarr_redownload as _try_sonarr_redownload,
)
from mediaman.web.routes.library_api.redownload import (
    validate_redownload_body as _validate_redownload_body,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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

# Per-admin cap on redownload triggers.  Each call spawns an Arr lookup +
# add_movie/add_series round-trip, so a tighter burst cap than the search
# path: 20 per minute / 200 per day per actor.
_REDOWNLOAD_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=200,
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


def _resolve_keep_decision(duration: str, now: datetime) -> tuple[str, str | None, str]:
    """Translate the *duration* form value into ``(action, execute_at, label)``."""
    if duration == "forever":
        return ACTION_PROTECTED_FOREVER, None, "forever"
    days = VALID_KEEP_DURATIONS[duration]
    assert days is not None  # only "forever" maps to None and is handled above
    return ACTION_SNOOZED, (now + timedelta(days=int(days))).isoformat(), duration


def _apply_keep_action(
    conn,
    media_id: str,
    action: str,
    execute_at: str | None,
    now: datetime,
    snooze_label: str,
    username: str,
) -> None:
    """Upsert ``scheduled_actions`` + write the audit row in one transaction."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id = ? AND token_used = 0",
            (media_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE scheduled_actions "
                "SET action=?, execute_at=?, snoozed_at=?, snooze_duration=?, token_used=0 "
                "WHERE id=?",
                (action, execute_at, now.isoformat(), snooze_label, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO scheduled_actions "
                "(media_item_id, action, scheduled_at, execute_at, token, token_used, "
                "snoozed_at, snooze_duration) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    media_id,
                    action,
                    now.isoformat(),
                    execute_at,
                    secrets.token_urlsafe(32),
                    now.isoformat(),
                    snooze_label,
                ),
            )
        log_audit(
            conn,
            media_id,
            "snoozed",
            f"Kept for {snooze_label} by admin ({username})",
            actor=username,
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

    row = conn.execute("SELECT id FROM media_items WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        return respond_err("not_found", status=404)

    now = datetime.now(UTC)
    action, execute_at, snooze_label = _resolve_keep_decision(duration, now)
    _apply_keep_action(conn, media_id, action, execute_at, now, snooze_label, username)
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

    Two-phase, three-transaction layout (snapshot → Arr API call →
    completion).  The per-service Arr step is split across
    :func:`_handle_radarr_delete` and :func:`_handle_sonarr_delete`; the
    cleanup transaction lives in :func:`_finalise_media_delete`.  See the
    module-level rationale comment for why the intent-id thread must
    stay in this orchestrator.
    """
    if not _DELETE_LIMITER.check(username):
        logger.warning("media.delete_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many delete operations — slow down"
        )
    conn = get_db()

    snapshot, snap_err = _snapshot_media_for_delete(conn, media_id)
    if snap_err is not None:
        return snap_err
    assert snapshot is not None

    config = request.app.state.config
    is_movie = snapshot["media_type"] == "movie"
    intent_id: int | None = None
    if is_movie:
        client = build_radarr_from_db(conn, config.secret_key)
        if client:
            intent_id, err = _handle_radarr_delete(conn, client, media_id, snapshot)
            if err is not None:
                return err
    else:
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client:
            intent_id, err = _handle_sonarr_delete(conn, sonarr_client, media_id, snapshot)
            if err is not None:
                return err

    _finalise_media_delete(conn, media_id, snapshot, username)
    if intent_id is not None:
        _complete_delete_intent(conn, intent_id)
    logger.info(
        "Deleted %s (%s) — %s by %s",
        media_id,
        snapshot["title"],
        snapshot["file_path"],
        username,
    )
    return respond_ok({"id": media_id})


# rationale: orchestrates Radarr lookup → fallback to Sonarr lookup.  The
# per-service handlers live in :mod:`.redownload`; this orchestrator owns
# the rate-limit gate, the body validation, the fall-through to Sonarr on
# a Radarr miss, and the final 404-style response.
@router.post("/api/media/redownload")
def api_media_redownload(
    request: Request,
    body: _RedownloadRequest,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Re-download a deleted media item via Radarr or Sonarr."""
    if not _REDOWNLOAD_LIMITER.check(username):
        logger.warning("media.redownload_throttled user=%s", username)
        return JSONResponse(
            {"ok": False, "error": "Too many redownload requests — slow down"},
            status_code=429,
        )

    params, validation_err = _validate_redownload_body(body)
    if validation_err is not None:
        return validation_err
    assert params is not None

    conn = get_db()
    config = request.app.state.config

    radarr_client = build_radarr_from_db(conn, config.secret_key)
    radarr_response = _try_radarr_redownload(conn, radarr_client, params, username)
    if radarr_response is not None:
        return radarr_response

    sonarr_client = build_sonarr_from_db(conn, config.secret_key)
    sonarr_response = _try_sonarr_redownload(conn, sonarr_client, params, username)
    if sonarr_response is not None:
        return sonarr_response

    return JSONResponse(
        {"ok": False, "error": f"'{params['title']}' not found in Radarr or Sonarr"}
    )


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
