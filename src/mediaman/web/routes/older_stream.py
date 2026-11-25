"""What an older TV fetches, with a ticket and no session: a film as one MPEG-TS, a test picture.

The film is the stream's own segments, one after another, from a place on. It is converted while
it is sent, so nobody knows how many bytes it has: no byte range is offered, and the TV is told
so. To go to another place, the TV is handed the film anew from there.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from mediaman.db import get_db
from mediaman.services.cast.tickets import Ticket
from mediaman.services.dlna.media import Media
from mediaman.services.media_meta.plex_stream import StreamGone, get_stream_manager
from mediaman.services.media_meta.plex_watch import PlexError, PlexUnavailable
from mediaman.web.responses import respond_err
from mediaman.web.routes.cast import _is_good, _read_ticket, _refused, _stream_lives
from mediaman.web.routes.older_reach import are_on
from mediaman.web.routes.watch.stream_api import _read_segment, _StreamRoute, plex_failure

router = APIRouter(route_class=_StreamRoute)

_FROM_RE = re.compile(r"([0-9]{1,6})\.ts")
_SEGMENT_RE = re.compile(r"\./seg/([0-9]{1,6}\.ts)")
_FROM_START_RE = re.compile(r"npt=0*(:00:00)?(\.0+)?-")
_CHUNK = 64 * 1024
# How long after a segment that could not be read it is asked for again.
_WAITS = (0.0, 2.0, 5.0)
# A TV opens a film more than once, and does not always close it.
_MAX_OPEN = 3
_KEPT_FEEDS = 64

PICTURE_SECONDS = 120.0
_PICTURE = Path(__file__).parents[1] / "static" / "icons" / "tv-test.jpg"


def film_media(url: str, title: str = "", seconds: int | None = None) -> Media:
    return Media(url=url, title=title, seconds=seconds)


def picture_media(url: str) -> Media:
    return Media(url=url, title="mediaman", mime="image/jpeg", converted=False)


def segments(stream_id: str, user: str) -> list[tuple[str, float]]:
    """The names of the stream's segments, and where in the title each begins."""
    text = get_stream_manager().playlist(stream_id, user, base_path=".")
    found: list[tuple[str, float]] = []
    at = length = 0.0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            length = float(line[len("#EXTINF:") : -1])
        elif not line.startswith("#"):
            match = _SEGMENT_RE.fullmatch(line)
            if match is None:
                raise PlexUnavailable("Plex sent no MPEG-TS")
            found.append((match[1], at))
            at += length
    if not found or "#EXT-X-MAP" in text:
        raise PlexUnavailable("Plex sent no MPEG-TS")
    return found


def place_of(parts: list[tuple[str, float]], position_s: float) -> tuple[int, float]:
    """Which segment holds a place, and where that segment begins."""
    n = max((i for i, (_, at) in enumerate(parts) if at <= position_s), default=0)
    return n, parts[n][1]


@dataclass
class _Feed:
    """What a TV was sent of a stream, as of its latest request."""

    open: int = 0
    done: bool = False
    broke_off: bool = False


_feeds: dict[str, _Feed] = {}


def sent(stream_id: str) -> tuple[bool, bool]:
    feed = _feeds.get(stream_id, _Feed())
    return feed.done, feed.broke_off


def _feed(stream_id: str) -> _Feed:
    if stream_id not in _feeds:
        for old in [k for k, f in _feeds.items() if not f.open][
            : max(0, len(_feeds) - _KEPT_FEEDS)
        ]:
            del _feeds[old]
        _feeds[stream_id] = _Feed()
    return _feeds[stream_id]


def _may_go_on(ticket: Ticket) -> bool:
    return are_on(get_db()) and _is_good(ticket) and _stream_lives(ticket)


def _film_ticket(request: Request, token: str) -> Ticket | Response:
    if not are_on(get_db()):
        return _refused(request)
    return _read_ticket(request, token)


@router.api_route("/api/cast/{ticket}/from/{place}", methods=["GET", "HEAD"])
async def film_from(request: Request, ticket: str, place: str) -> Response:
    """The film from its segment number *place* on."""
    held = await run_in_threadpool(_film_ticket, request, ticket)
    if isinstance(held, Response):
        return held
    match = _FROM_RE.fullmatch(place)
    if match is None:
        return _refused(request)
    asked = request.headers.get("timeseekrange.dlna.org")
    if asked is not None and not _FROM_START_RE.match(asked.strip()):
        return respond_err("no_time_seek", status=406)
    headers = {
        "Accept-Ranges": "none",
        **film_media("http://tv/").stream_headers(request.headers),
    }
    if request.method == "HEAD":
        response = Response(media_type="video/mpeg", headers=headers)
        # Nobody knows the length.
        del response.headers["content-length"]
        return response
    try:
        parts = await run_in_threadpool(segments, held.stream_id, held.user)
    except StreamGone:
        return _refused(request)
    except PlexError as exc:
        return plex_failure(exc)
    names = [name for name, _ in parts[int(match[1]) :]]
    if not names:
        return _refused(request)
    feed = _feed(held.stream_id)
    if feed.open >= _MAX_OPEN:
        return respond_err("rate_limited", status=429)
    feed.done = feed.broke_off = False
    return StreamingResponse(
        _film(request, held, names, feed), media_type="video/mpeg", headers=headers
    )


async def _film(
    request: Request, held: Ticket, names: list[str], feed: _Feed
) -> AsyncIterator[bytes]:
    feed.open += 1
    try:
        for name in names:
            body = await _segment(request, held, name)
            if body is None:
                feed.broke_off = True
                return
            for start in range(0, len(body), _CHUNK):
                yield body[start : start + _CHUNK]
        feed.done = True
    finally:
        feed.open -= 1


async def _segment(request: Request, held: Ticket, name: str) -> bytes | None:
    for wait in _WAITS:
        await asyncio.sleep(wait)
        if not await run_in_threadpool(_may_go_on, held):
            return None
        try:
            answer = await _read_segment(request, held.stream_id, held.user, name)
        except StreamGone:
            return None
        if answer.status_code == 200:
            return bytes(answer.body)
    return None


# ── the test picture ──


@dataclass
class Picture:
    """A test picture one TV is asked to fetch."""

    expires: float
    fetched: threading.Event = field(default_factory=threading.Event)


_pictures: dict[str, Picture] = {}
_pictures_lock = threading.Lock()


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def offer_picture() -> tuple[str, Picture]:
    """A new test picture, and the token that fetches it."""
    token, picture = secrets.token_urlsafe(32), Picture(time.monotonic() + PICTURE_SECONDS)
    with _pictures_lock:
        _pictures[_key(token)] = picture
    return token, picture


def withdraw_picture(token: str) -> None:
    with _pictures_lock:
        _pictures.pop(_key(token), None)


@router.api_route("/api/cast/{ticket}/test.jpg", methods=["GET", "HEAD"])
def test_picture(request: Request, ticket: str) -> Response:
    with _pictures_lock:
        picture = _pictures.get(_key(ticket))
    if picture is None or time.monotonic() > picture.expires:
        return _refused(request)
    if request.method == "GET":
        picture.fetched.set()
    headers = picture_media("http://tv/").stream_headers(request.headers)
    return Response(_PICTURE.read_bytes(), media_type="image/jpeg", headers=headers)
