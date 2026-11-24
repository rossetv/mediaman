"""Library JSON API endpoints (/api/library and /api/media/...), package root.

This module holds the keep and library-list routes plus the _KEEP_LIMITER
singleton. The redownload and delete routes live in the .redownload and .delete
submodules, whose sub-routers are mounted here. Delete-intent durability helpers
come from mediaman.web.repository.delete_intents. Names that tests patch
(build_radarr_from_db, build_sonarr_from_db, the *_LIMITER singletons) are
re-exported here so tests can patch them at this path.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import resolve_keep_decision
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.models import VALID_KEEP_DURATIONS
from mediaman.web.repository.delete_intents import (
    _complete_delete_intent as _complete_delete_intent,
)
from mediaman.web.repository.delete_intents import (
    _fail_delete_intent as _fail_delete_intent,
)
from mediaman.web.repository.delete_intents import (
    _record_delete_intent as _record_delete_intent,
)
from mediaman.web.repository.delete_intents import (
    reconcile_pending_delete_intents,
)
from mediaman.web.repository.kept import KeepRow
from mediaman.web.repository.library_api import (
    KeepChanged,
    NotFound,
    apply_keep_in_tx,
)
from mediaman.web.repository.library_query import (
    VALID_SORTS,
    VALID_TYPES,
    fetch_library,
    fetch_stats,
)
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.kept_show import keep_changed
from mediaman.web.routes.library_api._redownload_match import (
    _redownload_audit_id as _redownload_audit_id,
)
from mediaman.web.routes.library_api.delete import (
    _DELETE_LIMITER as _DELETE_LIMITER,
)
from mediaman.web.routes.library_api.delete import (
    api_media_delete,
)
from mediaman.web.routes.library_api.delete import router as _delete_router
from mediaman.web.routes.library_api.redownload import (
    _REDOWNLOAD_LIMITER as _REDOWNLOAD_LIMITER,
)
from mediaman.web.routes.library_api.redownload import (
    _RedownloadRequest as _RedownloadRequest,
)
from mediaman.web.routes.library_api.redownload import (
    api_media_redownload,
)
from mediaman.web.routes.library_api.redownload import router as _redownload_router

logger = logging.getLogger(__name__)

router = APIRouter()
router.include_router(_redownload_router)
router.include_router(_delete_router)


# Per-admin cap on keep/unkeep toggles.  These are cheap DB writes with no
# downstream Arr round-trip, so a generous burst cap is fine: 60 per minute
# / 500 per day per actor.
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
            "stats": fetch_stats(conn),
        }
    )


@router.post("/api/media/{media_id}/keep")
def api_media_keep(
    media_id: str,
    duration: str = Form(...),
    read_action: str = Form(""),
    read_execute_at: str = Form(""),
    read_kept_at: str = Form(""),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Keep an item. With *read_action*, the keep the person read ("none" for no keep), a keep
    that has changed since is left alone."""
    if not _KEEP_LIMITER.check(username):
        logger.warning("media.keep_throttled user=%s", _sanitise_log_field(username))
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
    # cannot race with a concurrent delete.
    try:
        deletion_running = apply_keep_in_tx(
            conn,
            media_id=media_id,
            action=decision.action,
            execute_at=decision.execute_at,
            now_iso=now.isoformat(),
            snooze_label=snooze_label,
            new_token=secrets.token_urlsafe(32),
            audit_detail=f"Kept for {snooze_label} by admin ({username})",
            actor=username,
            guarded=bool(read_action),
            read=None
            if read_action in ("", "none")
            else KeepRow(read_action, read_execute_at or None, read_kept_at or None),
        )
    except NotFound:
        return respond_err("not_found", status=404)
    except KeepChanged:
        return keep_changed()

    logger.info(
        "Media item %s protected for %s by %s",
        media_id,
        snooze_label,
        _sanitise_log_field(username),
    )

    body: dict[str, object] = {
        "id": media_id,
        "duration": snooze_label,
        "deletion_running": deletion_running,
        "keep": {
            "action": decision.action,
            "execute_at": decision.execute_at,
            "kept_at": now.isoformat(),
        },
    }
    if deletion_running:
        body["message"] = (
            "Kept, but this item was already being deleted. Its files may already "
            "be gone; it stays kept only if that deletion fails."
        )
    return respond_ok(body)


__all__ = [
    "api_library",
    "api_media_delete",
    "api_media_keep",
    "api_media_redownload",
    "build_radarr_from_db",
    "build_sonarr_from_db",
    "reconcile_pending_delete_intents",
    "router",
]
