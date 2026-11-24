"""Watch JSON endpoints: title search, Keep for a set number of days (and its Undo),
episode thumbnails.

Search and Keep read only items in the scanned libraries; a thumbnail is served
only for a Plex item in one of them. Plex addresses and tokens never reach the
response.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.infra import disk_cache
from mediaman.services.media_meta.plex_watch import (
    PlexItemNotFound,
    PlexNotConfigured,
    PlexUnavailable,
    is_in_libraries,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import apply_keep_snooze, revert_keep_snooze
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.models import VALID_KEEP_DURATIONS
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.kept import fetch_keeps
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.pages import (
    is_valid_key,
    movie_cards,
    open_plex,
    show_cards,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SEARCH_LIMIT = 60
_THUMB_CACHE = "private, max-age=86400"
_THUMB_FRESH_S = 7 * 86400
_THUMB_STALE_S = 30 * 86400

# Watch offers only the timed keeps, under the same labels the newsletter keep uses.
_KEEP_LABELS: dict[int, str] = {
    days: label for label, days in VALID_KEEP_DURATIONS.items() if days is not None
}

# Per-person cap on keeps; cheap DB writes, so the same burst the Library keep allows.
_KEEP_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60, max_per_day=500)

# Per-person cap on thumbnails fetched from Plex (a cached one is not counted), each of
# which costs two Plex calls; sized for a page that shows a season of them at once.
_THUMB_LIMITER = ActionRateLimiter(max_in_window=240, window_seconds=60)

# How long after a Keep its Undo still works. The keep overwrites the deletion date, so the
# old one is held here, in memory, for that long; after a restart the keep simply stays.
UNDO_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class _Undo:
    action_id: int
    kept_until: str  # the execute_at the keep wrote
    execute_at: str
    snoozed_at: str | None
    snooze_duration: str | None
    expires: datetime


_UNDO: dict[tuple[str, str], _Undo] = {}
_UNDO_LOCK = threading.Lock()


class _KeepBody(BaseModel):
    # Loosely typed on purpose: the handler checks both and answers a bad value with 400.
    media_item_id: Any = None
    days: Any = None


class _UnkeepBody(BaseModel):
    media_item_id: Any = None


@router.get("/api/watch/search")
def api_watch_search(q: str = "", username: str = Depends(get_current_user)) -> JSONResponse:
    term = q.strip()
    if not term:
        return JSONResponse({"items": [], "total": 0})
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    found, total = lib.search_library(conn, libraries, term, SEARCH_LIMIT)
    return JSONResponse({"items": library_items(conn, username, libraries, found), "total": total})


def library_items(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    titles: Sequence[lib.LibraryMovie | lib.LibraryShow],
) -> list[dict[str, Any]]:
    """The cards of library *titles*, in their order, each with its title ref."""
    now = now_utc()
    movies = [t for t in titles if isinstance(t, lib.LibraryMovie)]
    pending = lib.pending_deletions(conn, libraries, now)
    by_ref = {
        f"movie-{m.key}": c
        for m, c in zip(movies, movie_cards(conn, username, movies, pending, now), strict=True)
    }
    shows = [t for t in titles if isinstance(t, lib.LibraryShow)]
    by_ref |= {
        f"show-{s.key}": c
        for s, c in zip(shows, show_cards(conn, username, libraries, shows), strict=True)
    }
    refs = (
        f"movie-{t.key}" if isinstance(t, lib.LibraryMovie) else f"show-{t.key}" for t in titles
    )
    return [{**by_ref[ref], "ref": ref} for ref in refs]


@router.post("/api/watch/keep")
def api_watch_keep(
    body: _KeepBody,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """Keep a scheduled movie or season for 7, 30 or 90 days, as the newsletter keep does.

    An admin, who may change it, is told the keep; a viewer only that it was made.
    """
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
    reply: dict[str, object] = {}
    with conn:
        before = conn.execute(
            "SELECT execute_at, snoozed_at, snooze_duration FROM scheduled_actions WHERE id = ?",
            (pending.action_id,),
        ).fetchone()
        applied = before is not None and apply_keep_snooze(
            conn,
            action_id=pending.action_id,
            duration=duration,
            days=days,
            now=now,
            expected_execute_at=before["execute_at"],
        )
        if applied:
            _remember_undo(
                username,
                media_item_id,
                _Undo(
                    action_id=pending.action_id,
                    kept_until=(now + timedelta(days=days)).isoformat(),
                    execute_at=before["execute_at"],
                    snoozed_at=before["snoozed_at"],
                    snooze_duration=before["snooze_duration"],
                    expires=now + UNDO_WINDOW,
                ),
                now,
            )
            log_audit(
                conn,
                media_item_id,
                "snoozed",
                f"Kept for {duration} by {username}",
                actor=username,
            )
            if get_user_role(conn, username) == "admin":
                keep = fetch_keeps(conn, [media_item_id], now.isoformat()).get(media_item_id)
                reply["keep"] = asdict(keep) if keep else None
    if not applied:
        return respond_err("already_processed", status=409)
    logger.info(
        "watch.keep media_item=%s duration=%s user=%s",
        media_item_id,
        duration,
        _sanitise_log_field(username),
    )
    return respond_ok(reply)


def _remember_undo(username: str, media_item_id: str, undo: _Undo, now: datetime) -> None:
    with _UNDO_LOCK:
        for key in [k for k, v in _UNDO.items() if v.expires <= now]:
            del _UNDO[key]
        _UNDO[(username, media_item_id)] = undo


@router.post("/api/watch/unkeep")
def api_watch_unkeep(
    body: _UnkeepBody,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """Undo this person's Keep from the last few minutes: the deletion date it replaced
    comes back, and so does the emailed keep link."""
    if not _KEEP_LIMITER.check(username):
        return respond_err(
            "too_many_requests", status=429, message="Too many keep operations — slow down"
        )
    media_item_id = body.media_item_id
    if not isinstance(media_item_id, str) or not is_valid_key(media_item_id):
        return respond_err("invalid_request", status=400)

    now = now_utc()
    with _UNDO_LOCK:
        undo = _UNDO.pop((username, media_item_id), None)
    if undo is None or undo.expires <= now:
        return respond_err("too_late", status=409, message="This can no longer be undone.")

    conn = get_db()
    with conn:
        restored = revert_keep_snooze(
            conn,
            undo.action_id,
            kept_until=undo.kept_until,
            execute_at=undo.execute_at,
            snoozed_at=undo.snoozed_at,
            snooze_duration=undo.snooze_duration,
            now=now,
        )
        if restored:
            log_audit(
                conn,
                media_item_id,
                "unprotected",
                f"Keep undone by {username}",
                actor=username,
            )
    if not restored:
        return respond_err("too_late", status=409, message="This can no longer be undone.")
    logger.info("watch.unkeep media_item=%s user=%s", media_item_id, _sanitise_log_field(username))
    return respond_ok()


@router.get("/api/watch/thumb/{rating_key}")
def api_watch_thumb(
    request: Request, rating_key: str, username: str = Depends(get_current_user)
) -> Response:
    if not is_valid_key(rating_key):
        return Response(status_code=404)
    conn = get_db()
    libraries = {str(s) for s in lib.scanned_library_ids(conn)}
    try:
        plex = open_plex(request, conn)
    except PlexNotConfigured:
        return Response(status_code=404)

    def fetch() -> disk_cache.Image:
        item = plex.get_item(rating_key)
        if not is_in_libraries(item, libraries) or not item.thumb:
            raise PlexItemNotFound("Thumbnail not in the scanned libraries")
        content, content_type = plex.fetch_image(item.thumb)
        return content, {"mime": content_type, "section": str(item.library_section_id)}

    try:
        content, info = disk_cache.img().get_or_fetch(
            f"img:plexthumb:{rating_key}",
            _THUMB_FRESH_S,
            _THUMB_STALE_S,
            fetch,
            allow_fetch=lambda: _THUMB_LIMITER.check(username),
        )
    except disk_cache.Refused:
        return Response(status_code=429)
    except (PlexItemNotFound, PlexNotConfigured):
        return Response(status_code=404)
    except PlexUnavailable:
        return Response(status_code=503)
    # A cached thumb stays hidden once its library is no longer scanned.
    if info.get("section") not in libraries:
        return Response(status_code=404)
    return Response(
        content=content,
        media_type=info.get("mime", "image/jpeg"),
        headers={"Cache-Control": _THUMB_CACHE},
    )


__all__ = ["library_items", "router"]
