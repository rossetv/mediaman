"""Streams for a TV: started by a signed-in person, read by the TV with a ticket and no session.

A ticket reads its own stream's playlist and segments and nothing else. Whatever a ticket is
refused for, the answer is the same 404, so that a stranger learns nothing from it. Any origin
may read the ticket routes, since a TV's player runs on its maker's; none may send credentials.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Cookie, Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from mediaman.core.time import now_utc, parse_iso_strict_utc
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
)

router = APIRouter(route_class=_StreamRoute)

_PLAYLIST_TYPE = "application/vnd.apple.mpegurl"

# Several TVs of one home, each with a segment every few seconds, and a seek's burst.
_READ_LIMITER = RateLimiter(max_attempts=600, window_seconds=60)

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
        lambda manager, began: _start(request, body, username, session_hash, manager, began),
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
) -> Response:
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
    ticket = get_ticket_store().issue(stream_id, username, session_hash, live=_stream_lives)
    return respond_ok(
        {
            "stream_id": stream_id,
            "url": f"/api/cast/{ticket}/index.m3u8",
            "expires_in_s": int(TICKET_SECONDS),
            "duration_s": item.duration_s,
            "quality": body.quality,
            "audio": body.audio or _selected(audio),
            "subtitle": (subtitle or _selected(subtitles)) if burn else None,
            "audio_tracks": audio,
            "subtitle_tracks": subtitles,
        }
    )


def _stream_lives(ticket: Ticket) -> bool:
    try:
        get_stream_manager().peek(ticket.stream_id, ticket.user)
    except StreamGone:
        return False
    return True


def _may_still_watch(conn: sqlite3.Connection, ticket: Ticket) -> bool:
    """Whether the session that started the stream, and its user, are still good.

    Signing out, a new password and removing the user each end the session.
    """
    row = conn.execute(
        "SELECT expires_at FROM admin_sessions WHERE token_hash = ?", (ticket.session_hash,)
    ).fetchone()
    expires = parse_iso_strict_utc(row["expires_at"]) if row is not None else None
    return (
        expires is not None
        and expires >= now_utc()
        and not user_must_change_password(conn, ticket.user)
    )


def _read_ticket(request: Request, token: str) -> Ticket | None:
    """The one way a ticket is read; None for every refusal."""
    if not _READ_LIMITER.check(get_client_ip(request)):
        return None
    ticket = get_ticket_store().read(token)
    if ticket is None:
        return None
    if not _stream_lives(ticket) or not _may_still_watch(get_db(), ticket):
        get_ticket_store().drop(token)
        return None
    return ticket


def _refused() -> Response:
    return _for_tv(respond_err("not_found", status=404))


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
    if held is None:
        return _refused()
    if request.method == "HEAD":
        return _head(_PLAYLIST_TYPE)
    try:
        text = get_stream_manager().playlist(
            held.stream_id, held.user, base_path=f"/api/cast/{ticket}"
        )
    except StreamGone:
        return _refused()
    except PlexError as exc:
        return _for_tv(plex_failure(exc))
    return _for_tv(Response(text, media_type=_PLAYLIST_TYPE))


@router.api_route("/api/cast/{ticket}/seg/{name}", methods=["GET", "HEAD"])
async def cast_segment(request: Request, ticket: str, name: str) -> Response:
    held = await run_in_threadpool(_read_ticket, request, ticket)
    if held is None:
        return _refused()
    try:
        content_type = segment_type(name)
        if request.method == "HEAD":
            return _head(content_type)
        return _for_tv(await _read_segment(request, held.stream_id, held.user, name))
    except (ValueError, StreamGone):
        return _refused()
