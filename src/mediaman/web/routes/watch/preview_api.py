"""Frame previews for the Watch player's play bar.

Only frames Plex has already made, for the person who started the stream; looking at them does
not count as using the stream. Kept apart from the stream routes, whose answers are never stored.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from mediaman.db import get_db
from mediaman.services.infra import allowed_outbound_hosts, disk_cache
from mediaman.services.media_meta.plex_stream import StreamGone, get_stream_manager
from mediaman.services.media_meta.plex_watch import (
    PREVIEW_INTERVAL_S,
    PlexError,
    PlexNotConfigured,
    PlexWatch,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.responses import respond_err
from mediaman.web.routes.watch.stream_api import plex_failure, read_plex_credentials, stream_gone

router = APIRouter()

_FRESH_S = 30 * 86400
_HEADERS = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}

_LIMITER = ActionRateLimiter(max_in_window=120, window_seconds=60)
# Each miss holds a worker thread while Plex answers, and a drag along the bar makes many.
_FETCH_SLOTS = threading.BoundedSemaphore(4)


@router.get("/api/watch/stream/{stream_id}/preview/{n}")
def stream_preview(
    request: Request, stream_id: str, n: str, username: str = Depends(get_current_user)
) -> Response:
    """The frame at ``n`` times the preview interval."""
    try:
        stream = get_stream_manager().peek(stream_id, username)
    except StreamGone:
        return stream_gone()
    preview = stream.info.get("preview")
    if preview is None or not (n.isascii() and n.isdigit() and len(n) <= 5):
        return respond_err("not_found", status=404)
    number = int(n)
    if number >= preview["count"]:
        return respond_err("not_found", status=404)

    has_slot = False

    def allow_fetch() -> bool:
        nonlocal has_slot
        has_slot = _FETCH_SLOTS.acquire(blocking=False)
        return has_slot and _LIMITER.check(username)

    part_id, part_size = preview["part_id"], preview["part_size"]
    try:
        conn = get_db()
        creds = read_plex_credentials(conn, request.app.state.config.secret_key)
        if creds is None:
            raise PlexNotConfigured("Plex is not set up")
        plex = PlexWatch(*creds, allowed_hosts=allowed_outbound_hosts(conn))

        def fetch() -> disk_cache.Image:
            return plex.fetch_preview(part_id, number * PREVIEW_INTERVAL_S * 1000), {}

        content, _ = disk_cache.img().get_or_fetch(
            f"img:preview:{stream.rating_key}:{part_id}:{part_size}:{number}",
            _FRESH_S,
            _FRESH_S,
            fetch,
            allow_fetch=allow_fetch,
        )
    except disk_cache.Refused:
        return respond_err("rate_limited", status=429)
    except PlexError as exc:
        return plex_failure(exc)
    finally:
        if has_slot:
            _FETCH_SLOTS.release()
    return Response(content, media_type="image/jpeg", headers=_HEADERS)
