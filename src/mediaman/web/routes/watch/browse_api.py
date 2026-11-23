"""Watch JSON endpoints: title search, Keep for a set number of days, episode thumbnails.

Search and Keep read only items in the scanned libraries; a thumbnail is served
only for a Plex item in one of them. Plex addresses and tokens never reach the
response.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.media_meta.plex_watch import (
    PlexItemNotFound,
    PlexNotConfigured,
    PlexUnavailable,
    is_in_libraries,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import apply_keep_snooze
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.models import VALID_KEEP_DURATIONS
from mediaman.web.repository import watch_library as lib
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.pages import (
    is_valid_key,
    movie_cards,
    open_plex,
    show_card,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SEARCH_LIMIT = 60
_THUMB_CACHE = "private, max-age=86400"

# Watch offers only the timed keeps, under the same labels the newsletter keep uses.
_KEEP_LABELS: dict[int, str] = {
    days: label for label, days in VALID_KEEP_DURATIONS.items() if days is not None
}

# Per-person cap on keeps; cheap DB writes, so the same burst the Library keep allows.
_KEEP_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60, max_per_day=500)

# Per-person cap on thumbnails, each of which costs two Plex calls; sized for a page that
# shows a season of them at once.
_THUMB_LIMITER = ActionRateLimiter(max_in_window=240, window_seconds=60)


class _KeepBody(BaseModel):
    # Loosely typed on purpose: the handler checks both and answers a bad value with 400.
    media_item_id: Any = None
    days: Any = None


@router.get("/api/watch/search")
def api_watch_search(q: str = "", username: str = Depends(get_current_user)) -> JSONResponse:
    term = q.strip()
    if not term:
        return JSONResponse({"items": []})
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    now = now_utc()
    found = lib.search_library(conn, libraries, term, SEARCH_LIMIT)
    movies = [f for f in found if isinstance(f, lib.LibraryMovie)]
    pending = lib.pending_deletions(conn, libraries, now)
    by_key = {
        m.key: c
        for m, c in zip(movies, movie_cards(conn, username, movies, pending, now), strict=True)
    }
    items: list[dict[str, Any]] = [
        by_key[f.key] if isinstance(f, lib.LibraryMovie) else show_card(f) for f in found
    ]
    return JSONResponse({"items": items})


@router.post("/api/watch/keep")
def api_watch_keep(
    body: _KeepBody,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """Keep a scheduled movie or season for 7, 30 or 90 days, as the newsletter keep does."""
    if not _KEEP_LIMITER.check(username):
        return respond_err(
            "too_many_requests", status=429, message="Too many keep operations — slow down"
        )
    media_item_id = body.media_item_id
    days = body.days
    if (
        not isinstance(media_item_id, str)
        or not is_valid_key(media_item_id)
        or isinstance(days, bool)
        or not isinstance(days, int)
        or days not in _KEEP_LABELS
    ):
        return respond_err("invalid_request", status=400)

    conn = get_db()
    now = now_utc()
    pending = lib.pending_deletion(conn, lib.scanned_library_ids(conn), media_item_id, now)
    if pending is None:
        return respond_err("not_found", status=404)

    duration = _KEEP_LABELS[days]
    with conn:
        applied = apply_keep_snooze(
            conn, action_id=pending.action_id, duration=duration, days=days, now=now
        )
        if applied:
            log_audit(
                conn,
                media_item_id,
                "snoozed",
                f"Kept for {duration} by {username}",
                actor=username,
            )
    if not applied:
        return respond_err("already_processed", status=409)
    logger.info(
        "watch.keep media_item=%s duration=%s user=%s",
        media_item_id,
        duration,
        _sanitise_log_field(username),
    )
    return respond_ok()


@router.get("/api/watch/thumb/{rating_key}")
def api_watch_thumb(
    request: Request, rating_key: str, username: str = Depends(get_current_user)
) -> Response:
    if not is_valid_key(rating_key):
        return Response(status_code=404)
    if not _THUMB_LIMITER.check(username):
        return Response(status_code=429)
    conn = get_db()
    try:
        plex = open_plex(request, conn)
        item = plex.get_item(rating_key)
        if not is_in_libraries(item, lib.scanned_library_ids(conn)) or not item.thumb:
            return Response(status_code=404)
        content, content_type = plex.fetch_image(item.thumb)
    except (PlexItemNotFound, PlexNotConfigured):
        return Response(status_code=404)
    except PlexUnavailable:
        return Response(status_code=503)
    return Response(
        content=content, media_type=content_type, headers={"Cache-Control": _THUMB_CACHE}
    )


__all__ = ["router"]
