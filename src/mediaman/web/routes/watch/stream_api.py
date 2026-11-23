"""Watch stream and progress API: Plex streams proxied per person, progress kept by mediaman.

The browser only ever sees ``/api/watch/stream/<stream_id>/...`` paths. Every stream route
answers for the person who started the stream; any other id, someone else's included, is
410 Gone with the same body. Plex errors are mapped to short codes so no Plex address,
session or token reaches a response or a log line.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import Response

from mediaman.core.scrub_filter import register_secret
from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.infra import allowed_outbound_hosts
from mediaman.services.infra.settings_reader import get_setting, get_string_setting
from mediaman.services.media_meta.plex_stream import (
    StreamGone,
    TooManyStreams,
    get_stream_manager,
)
from mediaman.services.media_meta.plex_watch import (
    PlexError,
    PlexItemNotFound,
    PlexNotConfigured,
    PlexWatch,
    is_in_libraries,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.repository.watch_progress import save_progress
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

# A CDN or reverse proxy in front of mediaman must neither store nor re-encode video, and
# nginx must not buffer it.
_STREAM_HEADERS = {"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"}

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
    quality: Literal["best", "medium", "low"]


class _ProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream_id: str = Field(min_length=1, max_length=64)
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


def _gone() -> Response:
    return respond_err("gone", status=410)


def _plex_failure(exc: PlexError) -> Response:
    if isinstance(exc, PlexItemNotFound):
        return respond_err("not_found", status=404)
    if isinstance(exc, PlexNotConfigured):
        return respond_err("not_set_up", status=503)
    logger.warning("watch.stream: Plex call failed (%s)", type(exc).__name__)
    return respond_err("unreachable", status=502)


@router.post("/api/watch/stream")
def start_stream(
    request: Request,
    body: _StartRequest,
    username: str = Depends(get_current_admin),
) -> Response:
    """Start a stream of a movie or episode from offset 0 for the signed-in person."""
    if not _START_LIMITER.check(username):
        return respond_err("rate_limited", status=429)
    conn = get_db()
    try:
        creds = read_plex_credentials(conn, request.app.state.config.secret_key)
        if creds is None:
            raise PlexNotConfigured("Plex is not set up")
        plex = PlexWatch(*creds, allowed_hosts=allowed_outbound_hosts(conn))
        item = plex.get_item(body.rating_key)
        if item.type not in ("movie", "episode") or not is_in_libraries(
            item, _scanned_library_ids(conn)
        ):
            return respond_err("not_found", status=404)
        if item.type == "movie":
            info = {
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
        stream_id = get_stream_manager().start(
            username, body.rating_key, body.quality, item.subtitle_selected, info
        )
    except TooManyStreams:
        return respond_err("too_many_streams", status=429)
    except PlexError as exc:
        return _plex_failure(exc)
    return respond_ok(
        {"stream_id": stream_id, "playlist": f"/api/watch/stream/{stream_id}/index.m3u8"}
    )


@router.get("/api/watch/stream/{stream_id}/index.m3u8")
def stream_playlist(stream_id: str, username: str = Depends(get_current_admin)) -> Response:
    try:
        text = get_stream_manager().playlist(stream_id, username)
    except StreamGone:
        return _gone()
    except PlexError as exc:
        return _plex_failure(exc)
    return Response(text, media_type="application/vnd.apple.mpegurl")


@router.get("/api/watch/stream/{stream_id}/seg/{name}")
def stream_segment(
    stream_id: str, name: str, username: str = Depends(get_current_admin)
) -> Response:
    try:
        body, content_type = get_stream_manager().segment(stream_id, username, name)
    except ValueError:
        return respond_err("not_found", status=404)
    except StreamGone:
        return _gone()
    except PlexError as exc:
        return _plex_failure(exc)
    return Response(body, media_type=content_type)


@router.post("/api/watch/stream/{stream_id}/stop", status_code=204)
def stop_stream(stream_id: str, username: str = Depends(get_current_admin)) -> Response:
    """Stop the stream; one that is already gone is not an error."""
    with contextlib.suppress(StreamGone):
        get_stream_manager().stop(stream_id, username)
    return Response(status_code=204)


async def _progress_body(request: Request) -> _ProgressRequest:
    """Parse the body as JSON whatever its content type, since a beacon may arrive as text."""
    try:
        return _ProgressRequest.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid progress body") from None


@router.post("/api/watch/progress")
def save_watch_progress(
    username: Annotated[str, Depends(get_current_admin)],
    body: Annotated[_ProgressRequest, Depends(_progress_body)],
) -> Response:
    """Save the person's position; the item and its duration come from their stream."""
    try:
        stream = get_stream_manager().get(body.stream_id, username)
    except StreamGone:
        return _gone()
    info = stream.info
    watched = save_progress(
        get_db(),
        username=username,
        rating_key=stream.rating_key,
        media_item_id=info.get("media_item_id"),
        show_rating_key=info.get("show_rating_key"),
        position_s=body.position_s,
        duration_s=info.get("duration_s") or 0,
        played_s=body.played_s,
        now=now_utc(),
    )
    return respond_ok({"watched": watched})
