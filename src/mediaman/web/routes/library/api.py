"""Library JSON API endpoints — orchestration and keep/list handlers.

The delete-intent state machine and the redownload flow live in
sibling modules:

* :mod:`._intent`     — POST /api/media/{id}/delete + reconcile helper
* :mod:`._redownload` — POST /api/media/redownload + lookup helpers

This module retains:

* GET  /api/library
* POST /api/media/{id}/keep

It also re-exports the public-ish names from the sibling modules so
existing imports of the form
``from mediaman.web.routes.library.api import X`` continue to work, and
test patches against ``mediaman.web.routes.library.api.build_radarr_from_db``
/ ``build_sonarr_from_db`` continue to take effect — the route handlers
in the sibling modules look those names up via this module at call time.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import JSONResponse

from mediaman.audit import log_audit
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED, VALID_KEEP_DURATIONS

from ._intent import (
    _DELETE_LIMITER,
    _complete_delete_intent,
    _fail_delete_intent,
    _record_delete_intent,
    api_media_delete,
    reconcile_pending_delete_intents,
)
from ._query import _VALID_SORTS, _VALID_TYPES, fetch_library
from ._redownload import (
    _REDOWNLOAD_LIMITER,
    _REDOWNLOAD_TITLE_SIMILARITY,
    _pick_lookup_match,
    _redownload_audit_id,
    _RedownloadRequest,
    api_media_redownload,
)

logger = logging.getLogger("mediaman")

router = APIRouter()


# Per-admin cap on keep/snooze actions.
_KEEP_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)


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
        return JSONResponse(
            {"error": "Too many keep operations — slow down"},
            status_code=429,
        )

    conn = get_db()

    if duration not in VALID_KEEP_DURATIONS:
        return JSONResponse({"error": "Invalid duration"}, status_code=400)

    row = conn.execute("SELECT id FROM media_items WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": "Not found"}, status_code=404)

    now = datetime.now(UTC)

    if duration == "forever":
        action = ACTION_PROTECTED_FOREVER
        execute_at = None
        snooze_label = "forever"
    else:
        days = VALID_KEEP_DURATIONS[duration]
        assert days is not None  # only "forever" maps to None and is handled above
        action = ACTION_SNOOZED
        execute_at = (now + timedelta(days=int(days))).isoformat()
        snooze_label = duration

    conn.execute("BEGIN IMMEDIATE")
    try:
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
    except Exception:
        conn.execute("ROLLBACK")
        raise

    log_audit(
        conn,
        media_id,
        "snoozed",
        f"Kept for {snooze_label} by admin ({username})",
        actor=username,
    )

    conn.commit()
    logger.info("Media item %s protected for %s by %s", media_id, snooze_label, username)

    return JSONResponse({"ok": True, "id": media_id, "duration": snooze_label})


# Re-exported names. Listed explicitly so static analysis is happy and
# the public surface of this module is documented in one place.
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
