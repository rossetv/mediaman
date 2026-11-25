"""Watch stream and progress API: Plex streams proxied per person, progress kept by mediaman.

The browser only ever sees ``/api/watch/stream/<stream_id>/...`` paths. Every stream route
answers for the person who started the stream; any other id, someone else's included, is
410 Gone with the same body, but for a stream of their own that a newer start of the same title
replaced, which says so. Plex errors are mapped to short codes so no Plex address,
session or token reaches a response or a log line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from mediaman.core.scrub_filter import register_secret
from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.infra import SafeHTTPClient, allowed_outbound_hosts
from mediaman.services.infra.settings_reader import get_setting, get_string_setting
from mediaman.services.media_meta.plex_quality import (
    ORIGINAL,
    QualityId,
    may_copy,
    quality_rows,
    resolve_quality,
    without_original,
)
from mediaman.services.media_meta.plex_stream import (
    SegmentRead,
    StreamGone,
    StreamInfo,
    StreamManager,
    StreamReplaced,
    TooManyReads,
    TooManyStarts,
    TooManyStreams,
    get_stream_manager,
)
from mediaman.services.media_meta.plex_watch import (
    PREVIEW_INTERVAL_S,
    PlexError,
    PlexItem,
    PlexItemNotFound,
    PlexNotConfigured,
    PlexWatch,
    is_in_libraries,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository.watch_progress import get_progress, save_progress
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

# A CDN or reverse proxy in front of mediaman must neither store nor re-encode video, and
# nginx must not buffer it.
_STREAM_HEADERS = {"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"}

# The player's played time can run a little ahead of the server's clock for the viewing.
_PLAYED_SLACK_SECONDS = 30.0

# A place this close to the saved one is the same place; a save is rounded and the video may snap.
_SAME_PLACE_SECONDS = 0.5

# A segment request past the limits waits this long for a slot, so a player that fetches a little
# ahead, or a seek whose old requests Plex still holds, is not refused. Well under the 10 s the
# player allows before the first byte.
_SLOT_WAIT_SECONDS = 4.0
_SLOT_POLL_SECONDS = 0.1
_MAX_WAITING_PER_STREAM = 4
_RETRY_AFTER_SECONDS = 2

#: stream id -> segment requests waiting for a slot; only touched on the event loop.
_waiting: Counter[str] = Counter()

# The question about the title, which giving up a start does not interrupt.
_ITEM_TIMEOUT = (3.0, 7.0)

_START_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60)


class _StreamRoute(APIRoute):
    """Route that puts the stream headers on every response its handler returns."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def with_stream_headers(request: Request) -> Response:
            response = await handler(request)
            response.headers.update(_STREAM_HEADERS)
            return response

        return with_stream_headers


router = APIRouter(route_class=_StreamRoute)


class _StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    rating_key: str = Field(pattern=r"^[0-9]{1,18}$")
    quality: QualityId
    #: The player that asks, by a name it made up, and how long ago its person asked it to play.
    #: Neither is needed: a start without a page always starts.
    page: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    asked_s: float = Field(default=0, ge=0)


class _ProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream_id: str = Field(min_length=1, max_length=64)
    position_s: float = Field(ge=0)
    played_s: float = Field(ge=0)


class _StopRequest(BaseModel):
    """The player's final position, sent with the stop so one beacon on leave saves it."""

    model_config = ConfigDict(extra="forbid")

    position_s: float = Field(ge=0)
    played_s: float = Field(ge=0)


def read_plex_credentials(conn: sqlite3.Connection, secret_key: str) -> tuple[str, str] | None:
    """Return the stored ``(plex_url, plex_token)``, or None when either is missing."""
    url = get_string_setting(conn, "plex_url")
    token = get_string_setting(conn, "plex_token", secret_key=secret_key)
    if not url or not token:
        return None
    register_secret(token)
    return url, token


def _scanned_library_ids(conn: sqlite3.Connection) -> list[str]:
    libs = get_setting(conn, "plex_libraries", default=[])
    return [str(v) for v in libs] if isinstance(libs, list) else []


def stream_gone() -> Response:
    return respond_err("gone", status=410)


def _gone_answer(exc: StreamGone) -> Response:
    if not isinstance(exc, StreamReplaced):
        return stream_gone()
    # The page's own name for itself, no secret: a player told of its own start goes on.
    return respond_err("replaced", status=410, **({"by": exc.by} if exc.by else {}))


def plex_failure(exc: PlexError) -> Response:
    if isinstance(exc, PlexItemNotFound):
        return respond_err("not_found", status=404)
    if isinstance(exc, PlexNotConfigured):
        return respond_err("not_set_up", status=503)
    logger.warning("watch.stream: Plex call failed (%s)", type(exc).__name__)
    return respond_err("unreachable", status=502)


def _credits_start(item: PlexItem) -> float | None:
    """Where *item*'s last final credits start; ones in its first half are not believed."""
    starts = [
        m.start_s
        for m in item.markers
        if m.kind == "credits" and m.final and item.duration_s / 2 < m.start_s < item.duration_s
    ]
    return max(starts, default=None)


def _may_play_original(manager: StreamManager, item: PlexItem, username: str) -> bool:
    media = item.media
    if media is None or not may_copy(item):
        return False
    return manager.copies_video(item.rating_key, f"{media.part_id}:{media.part_size}", username)


@router.post("/api/watch/stream")
def start_stream(
    request: Request,
    body: _StartRequest,
    username: str = Depends(get_current_user),
) -> Response:
    """Start a stream of a movie or episode from offset 0 for the signed-in person.

    A start the player made by itself is answered 410 ``replaced`` where the person has asked
    another page to play the title since.
    """
    manager = get_stream_manager()
    began = manager.now()
    if not _START_LIMITER.check(username):
        return respond_err("rate_limited", status=429)
    try:
        with manager.starting(username, body.rating_key, body.page, body.asked_s) as start:
            answer = _start(request, body, username, manager, began)
            if answer.status_code != 200:
                # The answer is the one of a start given up, not of what it ran into: the player
                # tries again by itself after a failure of Plex's.
                start.check()
            return answer
    except StreamReplaced as exc:
        return _gone_answer(exc)
    except TooManyStarts:
        refused = respond_err("rate_limited", status=429)
        refused.headers["Retry-After"] = str(_RETRY_AFTER_SECONDS)
        return refused


def _start(
    request: Request, body: _StartRequest, username: str, manager: StreamManager, began: float
) -> Response:
    conn = get_db()
    try:
        creds = read_plex_credentials(conn, request.app.state.config.secret_key)
        if creds is None:
            raise PlexNotConfigured("Plex is not set up")
        http = SafeHTTPClient(
            default_timeout=_ITEM_TIMEOUT, allowed_hosts=allowed_outbound_hosts(conn)
        )
        plex = PlexWatch(*creds, http=http)
        item = plex.get_item(body.rating_key, marks=True)
        if item.type not in ("movie", "episode") or not is_in_libraries(
            item, _scanned_library_ids(conn)
        ):
            return respond_err("not_found", status=404)
        if item.type == "movie":
            info: dict[str, Any] = {
                "duration_s": item.duration_s,
                "media_item_id": item.rating_key,
                "show_rating_key": None,
            }
        else:
            info = {
                "duration_s": item.duration_s,
                "media_item_id": item.parent_rating_key,
                "show_rating_key": item.grandparent_rating_key,
            }
        info["credits_s"] = _credits_start(item)
        if item.media is not None and item.preview_count:
            # Kept on the server for the preview route; none of it is in the answer.
            info["preview"] = {
                "part_id": item.media.part_id,
                "part_size": item.media.part_size,
                "count": item.preview_count,
            }
        rows = quality_rows(item, original=_may_play_original(manager, item, username))
        quality = resolve_quality(rows, body.quality)
        stream_id = manager.start(
            username,
            body.rating_key,
            quality,
            item.subtitle_selected,
            info,
            fallback=resolve_quality(without_original(rows), ORIGINAL),
            page=body.page,
            # Plex was asked about the title first, and that may have taken long.
            asked_ago=body.asked_s + manager.now() - began,
        )
    except TooManyStreams:
        return respond_err("too_many_streams", status=429)
    except StreamReplaced as exc:
        return _gone_answer(exc)
    except PlexError as exc:
        return plex_failure(exc)
    with contextlib.suppress(StreamGone):
        playing = manager.peek(stream_id, username).quality
        if playing != quality:
            quality, rows = playing, without_original(rows)
    return respond_ok(
        {
            "stream_id": stream_id,
            "playlist": f"/api/watch/stream/{stream_id}/index.m3u8",
            "quality": quality,
            "qualities": rows,
            "markers": [asdict(m) for m in item.markers],
            "chapters": [asdict(c) for c in item.chapters],
            "previews": {"interval_s": PREVIEW_INTERVAL_S, "count": item.preview_count}
            if item.preview_count
            else None,
        }
    )


@router.get("/api/watch/stream/{stream_id}/index.m3u8")
def stream_playlist(stream_id: str, username: str = Depends(get_current_user)) -> Response:
    try:
        text = get_stream_manager().playlist(stream_id, username)
    except StreamGone as exc:
        return _gone_answer(exc)
    except PlexError as exc:
        return plex_failure(exc)
    return Response(text, media_type="application/vnd.apple.mpegurl")


async def _cancel_on_disconnect(request: Request, read: SegmentRead) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass
    read.cancel()


async def _claim_read(manager: StreamManager, read: SegmentRead) -> None:
    """Take a slot for *read*, waiting a little for one to come free.

    Waits on the event loop, so a request that waits holds none of the threads the pages use.
    """
    stream_id = read.stream_id
    try:
        return manager.claim_read(read)
    except TooManyReads:
        if _waiting[stream_id] >= _MAX_WAITING_PER_STREAM:
            raise
    _waiting[stream_id] += 1
    try:
        end = asyncio.get_running_loop().time() + _SLOT_WAIT_SECONDS
        while True:
            await asyncio.sleep(_SLOT_POLL_SECONDS)
            try:
                return manager.claim_read(read)
            except TooManyReads:
                if read.unwanted or asyncio.get_running_loop().time() >= end:
                    raise
    finally:
        _waiting[stream_id] -= 1
        if not _waiting[stream_id]:
            del _waiting[stream_id]


@router.get("/api/watch/stream/{stream_id}/seg/{name}")
async def stream_segment(
    request: Request, stream_id: str, name: str, username: str = Depends(get_current_user)
) -> Response:
    manager = get_stream_manager()
    read = SegmentRead(stream_id, username)
    watch = asyncio.create_task(_cancel_on_disconnect(request, read))
    try:
        await _claim_read(manager, read)
        try:
            body, content_type = await run_in_threadpool(manager.segment, read, name)
        finally:
            manager.release_read(read)
    except ValueError:
        return respond_err("not_found", status=404)
    except TooManyReads:
        refused = respond_err("rate_limited", status=429)
        refused.headers["Retry-After"] = str(_RETRY_AFTER_SECONDS)
        return refused
    except StreamGone as exc:
        return _gone_answer(exc)
    except PlexError as exc:
        return plex_failure(exc)
    finally:
        watch.cancel()
    return Response(body, media_type=content_type)


async def _stop_body(request: Request) -> _StopRequest | None:
    """The optional final position; a missing or malformed body is ignored."""
    raw = await request.body()
    if not raw:
        return None
    try:
        return _StopRequest.model_validate_json(raw)
    except ValidationError:
        return None


@router.post("/api/watch/stream/{stream_id}/stop", status_code=204)
def stop_stream(
    stream_id: str,
    username: Annotated[str, Depends(get_current_user)],
    final: Annotated[_StopRequest | None, Depends(_stop_body)],
) -> Response:
    """Stop the stream; one that is already gone is not an error.

    A body with the final position and played time is saved first, as progress would be,
    even when the reaper has just stopped the stream. A missing or malformed body never keeps
    the stream from stopping.
    """
    manager = get_stream_manager()
    if final is not None:
        with contextlib.suppress(StreamGone):
            try:
                stream = manager.get(stream_id, username)
            except StreamGone:
                stream = manager.reaped(stream_id, username)
            _save(stream, username, final.position_s, final.played_s)
    with contextlib.suppress(StreamGone):
        manager.stop(stream_id, username)
    return Response(status_code=204)


def _save(stream: StreamInfo, username: str, position_s: float, played_s: float) -> bool:
    info = stream.info
    conn = get_db()
    row = get_progress(conn, username, [stream.rating_key]).get(stream.rating_key)
    if row is not None and played_s <= stream.saved_played_s:
        # What this viewing saved where it has: a mark may have moved the row's own place.
        place = stream.saved_position_s
        if place is None:
            place = row.position_s
        if abs(position_s - place) < _SAME_PLACE_SECONDS:
            # A paused player left open: saving again would bring a dismissed title back, or
            # undo a mark made meanwhile.
            return row.watched
    watched = save_progress(
        conn,
        username=username,
        rating_key=stream.rating_key,
        media_item_id=info.get("media_item_id"),
        show_rating_key=info.get("show_rating_key"),
        position_s=position_s,
        duration_s=info.get("duration_s") or 0,
        # played_s is the client's own count, so it cannot exceed the time the viewing has run.
        played_s=min(played_s, stream.elapsed_s + _PLAYED_SLACK_SECONDS),
        now=now_utc(),
        credits_s=info.get("credits_s"),
    )
    get_stream_manager().note_saved(username, stream, position_s, played_s)
    return watched


async def _progress_body(request: Request) -> _ProgressRequest:
    """Parse the body as JSON whatever its content type, since a beacon may arrive as text."""
    try:
        return _ProgressRequest.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid progress body") from None


@router.post("/api/watch/progress")
def save_watch_progress(
    username: Annotated[str, Depends(get_current_user)],
    body: Annotated[_ProgressRequest, Depends(_progress_body)],
) -> Response:
    """Save the person's position; the item and its duration come from their stream.

    A save for a stream the reaper stopped lately is kept, and still answered 410 so the
    player starts a new stream.
    """
    try:
        stream = get_stream_manager().get(body.stream_id, username)
    except StreamGone as exc:
        with contextlib.suppress(StreamGone):
            stream = get_stream_manager().reaped(body.stream_id, username)
            _save(stream, username, body.position_s, body.played_s)
        return _gone_answer(exc)
    return respond_ok({"watched": _save(stream, username, body.position_s, body.played_s)})
