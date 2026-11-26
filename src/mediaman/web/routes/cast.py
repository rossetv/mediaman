"""Streams for a TV: started by a signed-in person, read by the TV with a ticket and no session.

A ticket reads its own stream's playlist and segments and nothing else. Whatever a ticket is
refused for, the answer is the same 404 (429 once an address fails too often), so that a stranger
learns nothing from it. Any origin may read the ticket routes, since a TV's player runs on its
maker's; none may send credentials.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import asdict
from functools import partial

from fastapi import APIRouter, Cookie, Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from mediaman.db import get_db
from mediaman.services.cast.tickets import TICKET_SECONDS, Ticket, get_ticket_store
from mediaman.services.cast.tracks import Track, read_tracks
from mediaman.services.media_meta.plex_quality import TvQuality
from mediaman.services.media_meta.plex_stream import (
    StreamGone,
    StreamManager,
    StreamReplaced,
    TooManyStreams,
    TrackUnavailable,
    TvTracks,
    get_stream_manager,
    segment_type,
)
from mediaman.services.media_meta.plex_watch import PlexError, is_in_libraries
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.auth._token_hashing import hash_token
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import user_must_change_password
from mediaman.web.repository.tv import is_paired
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.stream_api import (
    _gone_answer,
    _guarded_start,
    _open_plex,
    _progress_info,
    _read_segment,
    _scanned_library_ids,
    _StartRequest,
    _StreamRoute,
    plex_failure,
    session_lasts,
)

router = APIRouter(route_class=_StreamRoute)

_PLAYLIST_TYPE = "application/vnd.apple.mpegurl"

# Keyed by stream: a segment every few seconds, and a seek's burst.
_STREAM_LIMITER = RateLimiter(max_attempts=240, window_seconds=60)
# Keyed by address, which the TVs behind a proxy share, so only failures count.
_FAILURE_LIMITER = RateLimiter(max_attempts=60, window_seconds=60)

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Range",
    "Access-Control-Expose-Headers": "Content-Length, Content-Type",
    "Access-Control-Max-Age": "600",
}


class _CastRequest(_StartRequest):
    quality: TvQuality = "auto"
    #: Track ids as an earlier answer gave them; without one, what is selected in Plex.
    audio: str | None = Field(default=None, pattern=r"^[0-9]{1,18}$")
    subtitle: str | None = Field(default=None, pattern=r"^(off|[0-9]{1,18})$")


@router.post("/api/cast/stream")
def start_cast(
    request: Request,
    body: _CastRequest,
    username: str = Depends(get_current_user),
    session_token: str = Cookie(),
) -> Response:
    """Start a stream any TV can play and return the address the TV reads it at.

    It replaces the person's own stream of the same title, and counts as one of theirs.
    """
    session_hash = hash_token(session_token)
    return _guarded_start(
        body,
        username,
        lambda manager, began: _start(
            request, body, username, session_hash, manager, began, check=True
        ),
    )


def _selected(tracks: list[Track]) -> str | None:
    return next((t["id"] for t in tracks if t["selected"]), None)


def _start(
    request: Request,
    body: _CastRequest,
    username: str,
    session_hash: str,
    manager: StreamManager,
    began: float,
    *,
    check: bool = False,
) -> Response:
    """*check* refuses what the session no longer holds once it has started; a TV's start has
    checks of its own.
    """
    conn = get_db()
    try:
        plex = _open_plex(request, conn)
        item = plex.get_item(body.rating_key, marks=True)
        if item.type not in ("movie", "episode") or not is_in_libraries(
            item, _scanned_library_ids(conn)
        ):
            return respond_err("not_found", status=404)
        audio, subtitles = read_tracks(plex, body.rating_key)
        subtitle = None if body.subtitle == "off" else body.subtitle
        if (body.audio is not None and body.audio not in {t["id"] for t in audio}) or (
            subtitle is not None and subtitle not in {t["id"] for t in subtitles}
        ):
            return respond_err("track_unavailable", status=409)
        burn = subtitle is not None or (body.subtitle is None and item.subtitle_selected)
        quality = "best" if body.quality == "auto" else body.quality
        stream_id = manager.start(
            username,
            body.rating_key,
            quality,
            burn,
            _progress_info(item),
            page=body.page,
            asked_ago=body.asked_s + manager.now() - began,
            tv=TvTracks(audio=body.audio, subtitle=subtitle),
        )
    except TooManyStreams:
        return respond_err("too_many_streams", status=429)
    except TrackUnavailable:
        return respond_err("track_unavailable", status=409)
    except StreamReplaced as exc:
        return _gone_answer(exc)
    except PlexError as exc:
        return plex_failure(exc)
    store = get_ticket_store()
    token = store.issue(stream_id, username, session_hash, live=_stream_lives)
    ticket = store.read(token)
    try:
        if ticket is None:
            raise StreamGone("stream is gone")
        manager.allow(stream_id, username, partial(_holds, token))
        # After `allow`, so that the reaper sees what ends the session from here on.
        if check and not _is_good(ticket):
            store.drop(token)
            manager.end(stream_id, username)
            return respond_err("not_authenticated", status=401)
    except StreamGone as exc:
        # A later start of the title took its place meanwhile.
        store.drop(token)
        return _gone_answer(exc)
    return respond_ok(
        {
            "stream_id": stream_id,
            "url": f"/api/cast/{token}/index.m3u8",
            "expires_in_s": int(TICKET_SECONDS),
            "duration_s": item.duration_s,
            "quality": body.quality,
            "audio": body.audio or _selected(audio),
            "subtitle": (subtitle or _selected(subtitles)) if burn else None,
            "audio_tracks": audio,
            "subtitle_tracks": subtitles,
            "markers": [asdict(m) for m in item.markers],
            "chapters": [asdict(c) for c in item.chapters],
        }
    )


def _stream_lives(ticket: Ticket) -> bool:
    try:
        get_stream_manager().peek(ticket.stream_id, ticket.user)
    except StreamGone:
        return False
    return True


def _may_still_watch(conn: sqlite3.Connection, ticket: Ticket) -> bool:
    """Whether the session that started the stream, or the pairing of the TV it was started for,
    and its user are still good.

    Signing out, a new password and removing the user each end the session; unpairing and the
    last two each end the pairing.
    """
    return (
        session_lasts(conn, ticket.session_hash)
        or is_paired(conn, ticket.session_hash, ticket.user)
    ) and not user_must_change_password(conn, ticket.user)


def _is_good(ticket: Ticket) -> bool:
    return not get_ticket_store().expired(ticket) and _may_still_watch(get_db(), ticket)


def _holds(token: str) -> bool:
    """Whether the ticket *token* is still good, read again since a sign-in may have moved it."""
    ticket = get_ticket_store().read(token)
    return ticket is not None and _is_good(ticket)


def _read_ticket(request: Request, token: str) -> Ticket | Response:
    """The one way a ticket is read; the answer to give, where it may not be."""
    store = get_ticket_store()
    ticket = store.read(token)
    if ticket is not None and not _is_good(ticket):
        with contextlib.suppress(StreamGone):
            get_stream_manager().end(ticket.stream_id, ticket.user)
        store.drop(token)
        ticket = None
    if ticket is not None and not _stream_lives(ticket):
        store.drop(token)
        ticket = None
    if ticket is None:
        return _refused(request)
    if not _STREAM_LIMITER.check(ticket.stream_id):
        return _too_many(_STREAM_LIMITER.retry_after(ticket.stream_id))
    return ticket


def _refused(request: Request) -> Response:
    address = get_client_ip(request)
    if not _FAILURE_LIMITER.check(address):
        return _too_many(_FAILURE_LIMITER.retry_after(address))
    return _for_tv(respond_err("not_found", status=404))


def _too_many(retry_after: int) -> Response:
    response = _for_tv(respond_err("rate_limited", status=429))
    response.headers["Retry-After"] = str(retry_after)
    return response


def _for_tv(response: Response) -> Response:
    response.headers.update(_CORS)
    return response


def _head(content_type: str) -> Response:
    response = Response(media_type=content_type)
    # The length a GET would give is not known without asking Plex for it.
    del response.headers["content-length"]
    return _for_tv(response)


@router.options("/api/cast/{ticket}/index.m3u8")
@router.options("/api/cast/{ticket}/seg/{name}")
def cast_preflight() -> Response:
    return _for_tv(Response(status_code=204))


@router.api_route("/api/cast/{ticket}/index.m3u8", methods=["GET", "HEAD"])
def cast_playlist(request: Request, ticket: str) -> Response:
    held = _read_ticket(request, ticket)
    if isinstance(held, Response):
        return held
    if request.method == "HEAD":
        return _head(_PLAYLIST_TYPE)
    try:
        text = get_stream_manager().playlist(
            held.stream_id, held.user, base_path=f"/api/cast/{ticket}"
        )
    except StreamGone:
        return _refused(request)
    except PlexError as exc:
        return _for_tv(plex_failure(exc))
    return _for_tv(Response(text, media_type=_PLAYLIST_TYPE))


@router.api_route("/api/cast/{ticket}/seg/{name}", methods=["GET", "HEAD"])
async def cast_segment(request: Request, ticket: str, name: str) -> Response:
    held = await run_in_threadpool(_read_ticket, request, ticket)
    if isinstance(held, Response):
        return held
    try:
        content_type = segment_type(name)
        if request.method == "HEAD":
            return _head(content_type)
        return _for_tv(await _read_segment(request, held.stream_id, held.user, name))
    except (ValueError, StreamGone):
        return _refused(request)
