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
* :mod:`~mediaman.web.routes.library_api.delete` — the media delete
  pipeline (``POST /api/media/{id}/delete``), the Radarr/Sonarr delete
  branch helpers, and the ``_DELETE_LIMITER`` singleton. Its sub-router
  is mounted onto the package router below.
* This module (``__init__``) — keep / library-list routes plus the
  ``_KEEP_LIMITER`` singleton. Names that tests patch
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

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
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
)
from mediaman.web.repository.library_query import (
    VALID_SORTS,
    VALID_TYPES,
    fetch_library,
)
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.library_api.delete import (
    _DELETE_LIMITER,
    api_media_delete,
)
from mediaman.web.routes.library_api.delete import router as _delete_router
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
router.include_router(_delete_router)


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
