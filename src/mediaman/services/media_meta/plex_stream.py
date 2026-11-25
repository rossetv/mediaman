"""Plex universal-transcoder streams, proxied through mediaman and bound to the user who started them.

Only transcode sessions are touched on Plex (decision, start, playlist and segment reads, ping,
stop). Nothing is ever reported back to the account: no timeline, no scrobble, no settings, so
watched marks and resume positions on Plex stay as they were.

Plex session URLs work without a token, so they never leave this module: the browser sees only
``/api/watch/stream/<stream_id>/...`` paths, ``stream_id`` is unrelated to the Plex session id,
and every access is checked against the owning user. No exception message or log line carries
the Plex address, a session URL or the token.
"""

from __future__ import annotations

import contextlib
import http.client
import itertools
import json
import logging
import posixpath
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as ET
import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool, HTTPResponse, HTTPSConnectionPool
from urllib3.connection import HTTPConnection, HTTPSConnection

from mediaman import __version__
from mediaman.services.infra import resolve_safe_outbound_url
from mediaman.services.infra.http import ensure_hook_installed, pin
from mediaman.services.infra.http.streaming import (
    _ContentTypeMismatch,
    _read_capped,
    _SizeCapExceeded,
)
from mediaman.services.media_meta.plex_quality import ORIGINAL, QUALITY_PRESETS
from mediaman.services.media_meta.plex_watch import (
    _RATING_KEY_RE,
    PlexError,
    PlexNotConfigured,
    PlexUnavailable,
)

logger = logging.getLogger(__name__)

SESSION_PREFIX = "mediaman-"
IDLE_TIMEOUT_SECONDS = 180.0
SEGMENT_URL_PREFIX = "/api/watch/stream"

_CLIENT_IDENTIFIER = "mediaman-watch"
_USER_AGENT = f"mediaman/{__version__}"
_TRANSCODE = "/video/:/transcode/universal"
_TIMEOUT: tuple[float, float] = (5.0, 30.0)
# Stop and ping only nudge Plex, so a hung Plex must not hold them for the full read timeout.
_QUIET_TIMEOUT: tuple[float, float] = (5.0, 5.0)
# A start is several Plex calls in a row; the whole of it must end before a proxy in front of
# mediaman (Cloudflare's origin limit is 100 s) gives up on the request.
START_DEADLINE_SECONDS = 45.0
# One reaper round stops pinging once it has run this long, so idle streams are stopped on time.
REAP_PING_BUDGET_SECONDS = 60.0
SHUTDOWN_BUDGET_SECONDS = 5.0
_SEGMENT_MAX_BYTES = 32 * 1024 * 1024
_PLAYLIST_MAX_BYTES = 4 * 1024 * 1024

# How long after a user last touched an item a new stream of it still counts as the same viewing,
# so the time the player has already played carries over a restart, even after a long pause.
VIEWING_KEEP_SECONDS = 24 * 3600.0
# How long a stop or progress beacon that lost the race with the reaper still saves the position.
REAPED_KEEP_SECONDS = 600.0
# How long Plex's word on whether it would copy a file's video is kept.
COPY_VERDICT_KEEP_SECONDS = 3600.0
# How long a question Plex failed to answer is not asked again.
COPY_FAILURE_KEEP_SECONDS = 60.0

MAX_STREAMS_PER_USER = 3
MAX_STREAMS_TOTAL = 20
_POOL_MAXSIZE = 32

# A segment is read whole on a worker thread, and the pages share those (40 by default), so reads
# are few per stream and per user, and far fewer than the threads in all. A player reads one or
# two ahead.
MAX_READS_PER_STREAM = 2
MAX_READS_PER_USER = MAX_READS_PER_STREAM * MAX_STREAMS_PER_USER
MAX_READS_TOTAL = 16
# The whole of a segment read; the read timeout alone would let a trickling body hold its slot.
SEGMENT_DEADLINE_SECONDS = 60.0

_SEGMENT_NAME_RE = re.compile(r"[0-9]{1,6}\.(ts|m4s|mp4|vtt|aac)")
_VARIANT_RE = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*/index\.m3u8")
_EXTINF_RE = re.compile(r"#EXTINF:([0-9]{1,6}(?:\.[0-9]{1,9})?)(?:,.*)?")
_MAP_RE = re.compile(r'#EXT-X-MAP:URI="([^"]*)"')
_PLAIN_TAG_VALUE_RE = re.compile(r"[A-Za-z0-9.@-]*")
# Tags kept verbatim when their value is plain; any other tag or comment is dropped, since
# free text in a playlist could carry a Plex address or session path to the browser.
_PLAIN_TAGS = frozenset(
    {
        "#EXTM3U",
        "#EXT-X-VERSION",
        "#EXT-X-TARGETDURATION",
        "#EXT-X-MEDIA-SEQUENCE",
        "#EXT-X-DISCONTINUITY-SEQUENCE",
        "#EXT-X-PLAYLIST-TYPE",
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        "#EXT-X-BYTERANGE",
        "#EXT-X-ENDLIST",
    }
)
_CONTENT_TYPES = {
    "ts": "video/mp2t",
    "m4s": "video/iso.segment",
    "mp4": "video/mp4",
    "vtt": "text/vtt",
    "aac": "audio/aac",
}


class StreamGone(Exception):
    """The stream is unknown, belongs to someone else, or was stopped or reaped."""


class StreamReplaced(StreamGone):
    """The same user has started the same item anew, so this stream made way or stays ended.

    *by* is the page that plays the item now, where it gave its name.
    """

    def __init__(self, message: str, by: str | None = None) -> None:
        super().__init__(message)
        self.by = by


class _Unservable(PlexUnavailable):
    """Plex answered, and what it answered cannot be played: asking again soon changes nothing."""


class TooManyStreams(Exception):
    """The user, or mediaman as a whole, already has as many live streams as allowed."""


class TooManyReads(Exception):
    """The stream, its user or mediaman as a whole reads as many segments as allowed right now."""


#: Returns ``(plex_url, plex_token)``, or ``None`` when Plex is not set up.
CredentialsProvider = Callable[[], tuple[str, str] | None]

#: Outbound host allow-list: the set itself, or a callable returning it (read on every call).
AllowedHosts = frozenset[str] | Callable[[], frozenset[str]] | None


@dataclass(frozen=True)
class StreamInfo:
    rating_key: str
    quality: str
    info: Mapping[str, Any]
    #: Seconds since the viewing of this item began; it carries across restarts of the stream.
    elapsed_s: float = 0.0
    #: The viewing's played time as last saved.
    saved_played_s: float = 0.0
    #: The viewing's place as last saved, or None before its first save.
    saved_position_s: float | None = None


class SegmentRead:
    """One segment to read: ``claim_read`` gives it a slot, ``release_read`` takes it back."""

    def __init__(self, stream_id: str, user: str) -> None:
        self.stream_id = stream_id
        self.user = user
        self.unwanted = False
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None

    def cancel(self) -> None:
        """Say that nobody waits for the segment any more; wakes a thread that waits for Plex."""
        with self._lock:
            self.unwanted = True
            self._hang_up()

    def hold(self, sock: socket.socket) -> None:
        """Let ``cancel`` hang up *sock*; does so now if the read was cancelled already."""
        with self._lock:
            self._socket = sock
            if self.unwanted:
                self._hang_up()

    def _hang_up(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(OSError):
                # The plain socket's: TLS's own also drops state the reading thread is using.
                socket.socket.shutdown(self._socket, socket.SHUT_RDWR)


_reading = threading.local()


class _ReadResponse(http.client.HTTPResponse):
    def begin(self) -> None:
        super().begin()
        if getattr(_reading, "read", None) is not None:
            # Whatever Plex says of keeping the connection, it must never serve another request:
            # the read can hang it up until its body is in.
            self.will_close = True


class _ReadConnection(HTTPConnection):
    """Lets the read this thread is doing, if any, be cancelled while it waits for Plex.

    An outbound proxy's connections are not of this kind: there a cancelled read ends at the next
    chunk of its body.
    """

    response_class = _ReadResponse

    def getresponse(self) -> HTTPResponse:  # type: ignore[override]
        read: SegmentRead | None = getattr(_reading, "read", None)
        if read is not None:
            read.hold(self.sock)
        return super().getresponse()


class _ReadTLSConnection(_ReadConnection, HTTPSConnection):
    pass


class _ReadPool(HTTPConnectionPool):
    ConnectionCls = _ReadConnection


class _ReadTLSPool(HTTPSConnectionPool):
    ConnectionCls = _ReadTLSConnection


class _Adapter(HTTPAdapter):
    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _ReadPool, "https": _ReadTLSPool}


@dataclass
class _Viewing:
    """One user's viewing of one item, across restarts of its stream."""

    began: float
    touched: float
    saved_played_s: float = 0.0
    saved_position_s: float | None = None
    #: The latest start of the item that succeeded.
    started: int = -1
    #: The page that start came from, "" where it gave no name, and when its person asked for it.
    page: str = ""
    asked: float = float("-inf")


@dataclass
class _CopyVerdict:
    """Whether Plex would copy a file's video; *known* is False while Plex has not answered."""

    copied: bool
    known: bool
    until: float


@dataclass
class _Stream:
    user: str
    rating_key: str
    quality: str
    session_id: str
    child_path: str
    last_access: float
    viewing_started: float
    info: dict[str, Any] = field(default_factory=dict)
    #: Place of its start among all starts: of two of the same item, the later one plays.
    order: int = 0

    @property
    def item(self) -> tuple[str, str]:
        return self.user, self.rating_key


class _CheckedBody:
    """A Plex response that runs *check*, which may raise, before handing on each chunk."""

    def __init__(self, response: requests.Response, check: Callable[[], None]) -> None:
        self.headers = response.headers
        self._response = response
        self._check = check

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for chunk in self._response.iter_content(chunk_size=chunk_size):
            self._check()
            yield chunk


class StreamManager:
    """In-memory map of live streams, guarded by one lock and keyed by an opaque stream id.

    The lock is never held across a Plex call, so a slow Plex never blocks other users.
    """

    def __init__(
        self,
        credentials: CredentialsProvider,
        *,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        allowed_hosts: AllowedHosts = None,
    ) -> None:
        self._credentials = credentials
        self._allowed_hosts = allowed_hosts
        if session is None:
            session = requests.Session()
            adapter = _Adapter(pool_maxsize=_POOL_MAXSIZE)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        self._session = session
        self._clock = clock
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}
        # Plex session ids handed out and not yet stopped, including ones whose Plex session is
        # still opening and so not yet in ``_streams``; the startup tidy must leave them alone.
        self._issued: set[str] = set()
        # (user, rating_key) -> the viewing.
        self._viewings: dict[tuple[str, str], _Viewing] = {}
        # stream id -> (when it was reaped, the stream), kept as long as a viewing.
        self._reaped: dict[str, tuple[float, _Stream]] = {}
        # stream id -> (when a newer start of its item dropped it, its user and item, that start),
        # kept as long as a viewing.
        self._replaced: dict[str, tuple[float, tuple[str, str], int]] = {}
        # (user and item, start) of each start not yet through -> (its page, when it was asked for).
        self._opening: dict[tuple[tuple[str, str], int], tuple[str, float]] = {}
        self._starts = itertools.count()
        # (rating_key, file) -> Plex's word on copying its video.
        self._copies: dict[tuple[str, str], _CopyVerdict] = {}
        # An ended stream's reads stay here until Plex lets go of them, so starting anew is no
        # way around the limits.
        self._reads: set[SegmentRead] = set()

    def now(self) -> float:
        """The time on the manager's clock, in seconds from no set moment."""
        return self._clock()

    def start(
        self,
        user: str,
        rating_key: str,
        quality: str,
        burn_subtitles: bool,
        info: Mapping[str, Any] | None = None,
        *,
        fallback: str | None = None,
        page: str | None = None,
        asked_ago: float = 0.0,
    ) -> str:
        """Open a Plex transcode session at offset 0 and return the new stream id.

        An item plays on the page where its person last asked for it. *page* names the player
        that starts and *asked_ago* is how many seconds ago its person asked it to play: 0 for
        a press, more for what the player does by itself, such as trying again after a drop.
        A start asked for before another page's raises ``StreamReplaced`` and touches nothing.
        One with no *page* was asked for as it came, and always starts.

        A failed start is retried once with a fresh session, since Plex intermittently refuses
        a start right after another one for the same item. An original that Plex would not copy
        or could not start is retried as the *fallback* quality. The user's other streams of the
        same item are stopped first, since the player starts a new one on a quality change or
        restart; they count as replaced unless nothing could be started in their place. Of two
        starts of one item at the same time the later wins, and the other's id is returned as
        one already replaced.
        Raises ``ValueError`` for an unknown quality or a malformed rating key, and
        ``TooManyStreams`` when the user, or mediaman as a whole, is at the limit.
        """
        if quality not in QUALITY_PRESETS and quality != ORIGINAL:
            raise ValueError(f"unknown quality {quality!r}")
        if not _RATING_KEY_RE.fullmatch(rating_key):
            raise ValueError("rating key must be numeric")
        creds = self._creds()
        item = (user, rating_key)
        with self._lock:
            asked = self._clock() - asked_ago
            if page is not None:
                later = self._asked_later(item, page, asked)
                if later is not None:
                    raise StreamReplaced("another page was asked to play the item since", later)
            order = next(self._starts)
            dropped = self._replace_same_item(user, rating_key, order)
            self._opening[(item, order)] = (page or "", asked)
        try:
            for session_id in dropped.values():
                self._call_quietly("stop", session_id)
            return self._start_new(
                creds, user, rating_key, quality, burn_subtitles, info, fallback, order
            )
        finally:
            with self._lock:
                del self._opening[(item, order)]

    def _asked_later(self, item: tuple[str, str], page: str, asked: float) -> str | None:
        """The page other than *page* that was asked to play *item* after *asked*, "" where it
        gave no name; None where there is none. The lock must be held."""
        viewing = self._viewings.get(item)
        held = [(viewing.page, viewing.asked)] if viewing is not None else []
        held += [who for (i, _), who in self._opening.items() if i == item]
        others = [(at, other) for other, at in held if other != page and at > asked]
        return max(others)[1] if others else None

    def _playing_page(self, item: tuple[str, str]) -> str | None:
        """The page of the latest start of *item*, one still opening included; None where it
        gave no name. The lock must be held."""
        viewing = self._viewings.get(item)
        latest = (viewing.started, viewing.page) if viewing is not None else (-1, "")
        opening = [(order, who[0]) for (i, order), who in self._opening.items() if i == item]
        return max([latest, *opening])[1] or None

    def _start_new(
        self,
        creds: tuple[str, str],
        user: str,
        rating_key: str,
        quality: str,
        burn_subtitles: bool,
        info: Mapping[str, Any] | None,
        fallback: str | None,
        order: int,
    ) -> str:
        asked = quality
        base, token = creds
        with self._lock:
            self._check_room(user)
        deadline = self._clock() + START_DEADLINE_SECONDS
        refused = False
        for attempt in range(2):
            session_id = SESSION_PREFIX + secrets.token_urlsafe(16)
            with self._lock:
                self._issued.add(session_id)
            try:
                child_path = self._open_session(
                    base, token, session_id, rating_key, quality, burn_subtitles, deadline
                )
                break
            except PlexUnavailable as exc:
                refused = isinstance(exc, _Unservable)
                self._call_quietly("stop", session_id)
                if attempt == 1 or self._clock() >= deadline:
                    raise
                logger.info("plex_stream: start failed, retrying once")
                if quality == ORIGINAL and fallback is not None:
                    quality = fallback
            except BaseException:
                # Whatever failed, the Plex session may already be open: give it back.
                self._call_quietly("stop", session_id)
                raise
        stream_id = secrets.token_urlsafe(16)
        with self._lock:
            if quality != asked:
                keep = COPY_VERDICT_KEEP_SECONDS if refused else COPY_FAILURE_KEEP_SECONDS
                until = self._clock() + keep
                for key in self._copies:
                    if key[0] == rating_key:
                        self._copies[key] = _CopyVerdict(False, refused, until)
            # Another start of the same item may have stored its stream meanwhile.
            beaten = any(
                s.item == (user, rating_key) and s.order > order for s in self._streams.values()
            )
            if beaten:
                self._replaced[stream_id] = (self._clock(), (user, rating_key), order)
            else:
                replaced = self._replace_same_item(user, rating_key, order)
                try:
                    self._check_room(user)
                except TooManyStreams:
                    full = True
                else:
                    full = False
                    now = self._clock()
                    self._viewings = {
                        k: v
                        for k, v in self._viewings.items()
                        if now - v.touched <= VIEWING_KEEP_SECONDS
                    }
                    viewing = self._viewings.setdefault((user, rating_key), _Viewing(now, now))
                    viewing.touched = now
                    viewing.started = max(viewing.started, order)
                    viewing.page, viewing.asked = self._opening[((user, rating_key), order)]
                    self._streams[stream_id] = _Stream(
                        user=user,
                        rating_key=rating_key,
                        quality=quality,
                        session_id=session_id,
                        child_path=child_path,
                        last_access=now,
                        viewing_started=viewing.began,
                        info=dict(info or {}),
                        order=order,
                    )
        if beaten:
            self._call_quietly("stop", session_id)
            return stream_id
        for old_session in replaced.values():
            self._call_quietly("stop", old_session)
        if full:
            # Concurrent starts can all pass the first check; the one that loses gives its session back.
            self._call_quietly("stop", session_id)
            raise TooManyStreams("too many streams are open")
        return stream_id

    def _replace_same_item(self, user: str, rating_key: str, order: int) -> dict[str, str]:
        """Drop the user's streams of the item as replaced; return their Plex session ids by
        stream id. The lock must be held."""
        now = self._clock()
        self._replaced = {
            k: v for k, v in self._replaced.items() if now - v[0] <= VIEWING_KEEP_SECONDS
        }
        ids = [sid for sid, s in self._streams.items() if s.item == (user, rating_key)]
        self._replaced.update((sid, (now, (user, rating_key), order)) for sid in ids)
        return {sid: self._streams.pop(sid).session_id for sid in ids}

    def _check_room(self, user: str) -> None:
        """Raise ``TooManyStreams`` if a new stream would pass a limit; the lock must be held."""
        if len(self._streams) >= MAX_STREAMS_TOTAL:
            raise TooManyStreams("too many streams are open")
        if sum(1 for s in self._streams.values() if s.user == user) >= MAX_STREAMS_PER_USER:
            raise TooManyStreams(f"at most {MAX_STREAMS_PER_USER} streams can be open at once")

    def playlist(self, stream_id: str, user: str) -> str:
        """Return the media playlist with every URI rewritten to mediaman's segment route."""
        stream = self._touch(stream_id, user)
        base, token = self._creds()
        raw = self._get(
            base, token, stream.child_path, op="playlist", max_bytes=_PLAYLIST_MAX_BYTES
        )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise PlexUnavailable("Plex sent a playlist that is not text") from None
        return _rewrite_playlist(text, stream_id)

    def claim_read(self, read: SegmentRead) -> None:
        """Take a slot for *read*; give it back with ``release_read``.

        Never waits: raises ``TooManyReads`` when the stream, the user or mediaman as a whole is
        at the limit.
        """
        self._touch(read.stream_id, read.user)
        with self._lock:
            if (
                len(self._reads) >= MAX_READS_TOTAL
                or sum(r.user == read.user for r in self._reads) >= MAX_READS_PER_USER
                or sum(r.stream_id == read.stream_id for r in self._reads) >= MAX_READS_PER_STREAM
            ):
                raise TooManyReads("too many segments are being read")
            self._reads.add(read)

    def release_read(self, read: SegmentRead) -> None:
        with self._lock:
            self._reads.discard(read)

    def segment(self, read: SegmentRead, name: str) -> tuple[bytes, str]:
        """Fetch one segment whole and return ``(body, content_type)``.

        Raises ``ValueError`` for a name that is not a plain segment file name, and
        ``StreamGone`` once the stream ends or the read is cancelled.
        """
        if not _SEGMENT_NAME_RE.fullmatch(name):
            raise ValueError("not a segment name")
        stream = self._touch(read.stream_id, read.user)
        base, token = self._creds()
        path = f"{posixpath.dirname(stream.child_path)}/{name}"

        def still_wanted() -> None:
            self.peek(read.stream_id, read.user)
            if read.unwanted:
                raise StreamGone("the segment is no longer wanted")

        still_wanted()
        _reading.read = read
        try:
            body = self._get(
                base,
                token,
                path,
                op="segment",
                max_bytes=_SEGMENT_MAX_BYTES,
                deadline=self._clock() + SEGMENT_DEADLINE_SECONDS,
                check=still_wanted,
            )
        finally:
            _reading.read = None
        return body, _CONTENT_TYPES[name.rsplit(".", 1)[1]]

    def get(self, stream_id: str, user: str) -> StreamInfo:
        stream = self._touch(stream_id, user)
        with self._lock:
            return self._info(stream, self._clock())

    def _info(self, stream: _Stream, until: float) -> StreamInfo:
        """*stream* as callers see it, its time counted to *until*; the lock must be held."""
        viewing = self._viewings.get((stream.user, stream.rating_key))
        return StreamInfo(
            rating_key=stream.rating_key,
            quality=stream.quality,
            info=dict(stream.info),
            elapsed_s=until - stream.viewing_started,
            saved_played_s=viewing.saved_played_s if viewing else 0.0,
            saved_position_s=viewing.saved_position_s if viewing else None,
        )

    def note_saved(self, user: str, rating_key: str, position_s: float, played_s: float) -> None:
        """Remember the place and played time just saved for *user*'s viewing of the item."""
        with self._lock:
            viewing = self._viewings.get((user, rating_key))
            if viewing is not None:
                viewing.saved_position_s = position_s
                viewing.saved_played_s = played_s

    def _gone(self, stream_id: str, user: str) -> StreamGone:
        """Why *user* has no such stream; the lock must be held."""
        _, reaped = self._reaped.get(stream_id, (0.0, None))
        if reaped is not None and reaped.user == user:
            since = self._viewings.get(reaped.item)
            if since is not None and since.started > reaped.order:
                return StreamReplaced(
                    "a newer stream of the item was started since", self._playing_page(reaped.item)
                )
        _, item, order = self._replaced.get(stream_id, (0.0, ("", ""), 0))
        viewing = self._viewings.get(item)
        # A start that fails replaces nothing.
        taken = viewing is not None and viewing.started >= order
        if item[0] == user and (taken or any(i == item for i, _ in self._opening)):
            return StreamReplaced(
                "a newer stream of the item replaced this one", self._playing_page(item)
            )
        return StreamGone("stream is gone")

    def peek(self, stream_id: str, user: str) -> StreamInfo:
        """As ``get``, without counting as use of the stream, so an idle one still ends on time."""
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.user != user:
                raise self._gone(stream_id, user)
            return self._info(stream, stream.last_access)

    def copies_video(self, rating_key: str, file: str, user: str = "") -> bool:
        """Whether Plex would send this item's video as it is, remembered per *file* for a while.

        A restart of *user*'s viewing keeps the answer it has, however old. Only asks Plex for
        its decision, and stops the session the question named. When Plex cannot say, its
        earlier answer stands; without one, False.
        """
        if not _RATING_KEY_RE.fullmatch(rating_key):
            return False
        now = self._clock()
        with self._lock:
            held = self._copies.get((rating_key, file))
            viewing = self._viewings.get((user, rating_key))
        if held is not None:
            restart = viewing is not None and now - viewing.touched <= IDLE_TIMEOUT_SECONDS
            if now <= held.until or (held.known and restart):
                return held.copied
        session_id = SESSION_PREFIX + secrets.token_urlsafe(16)
        try:
            base, token = self._creds()
            body = self._get(
                base,
                token,
                f"{_TRANSCODE}/decision",
                op="decision",
                params=_session_params(session_id, rating_key, ORIGINAL, False),
                extra_headers={"X-Plex-Session-Identifier": session_id},
                max_bytes=_PLAYLIST_MAX_BYTES,
                timeout=_QUIET_TIMEOUT,
            )
        except PlexError:
            known = held is not None and held.known
            until = self._clock() + COPY_FAILURE_KEEP_SECONDS
            verdict = _CopyVerdict(held is not None and held.copied, known, until)
        else:
            self._call_quietly("stop", session_id)
            verdict = _CopyVerdict(_video_copied(body), True, now + COPY_VERDICT_KEEP_SECONDS)
        with self._lock:
            self._copies = {
                k: v for k, v in self._copies.items() if now - v.until <= VIEWING_KEEP_SECONDS
            }
            self._copies[(rating_key, file)] = verdict
        return verdict.copied

    def reaped(self, stream_id: str, user: str) -> StreamInfo:
        """Return a stream of *user*'s the reaper stopped lately, for a late save of its progress.

        Its played time ends at its last use, since nothing was played once it went idle.
        Raises ``StreamGone`` for any other stream, someone else's included, and while *user*
        has a live stream of the same item, whose newer position a late save would overwrite.
        """
        with self._lock:
            reaped_at, stream = self._reaped.get(stream_id, (0.0, None))
            if (
                stream is None
                or stream.user != user
                or self._clock() - reaped_at > REAPED_KEEP_SECONDS
                or any(
                    s.user == user and s.rating_key == stream.rating_key
                    for s in self._streams.values()
                )
            ):
                raise StreamGone("stream is gone")
            return self._info(stream, stream.last_access)

    def stop(self, stream_id: str, user: str) -> None:
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.user != user:
                raise StreamGone("stream is gone")
            del self._streams[stream_id]
        self._call_quietly("stop", stream.session_id)

    def reap(self, now: float | None = None) -> int:
        """Stop streams idle for longer than the timeout and ping the rest; return how many stopped.

        mediaman sends Plex no timeline updates, so the ping is what keeps a paused session alive.
        """
        now = self._clock() if now is None else now
        with self._lock:
            idle = [
                sid
                for sid, s in self._streams.items()
                if now - s.last_access > IDLE_TIMEOUT_SECONDS
            ]
            stopped = [self._streams.pop(sid) for sid in idle]
            self._reaped = {
                sid: kept
                for sid, kept in self._reaped.items()
                if now - kept[0] <= VIEWING_KEEP_SECONDS
            }
            self._reaped.update((sid, (now, s)) for sid, s in zip(idle, stopped, strict=True))
            live = [s.session_id for s in self._streams.values()]
        for stream in stopped:
            self._call_quietly("stop", stream.session_id)
        began = self._clock()
        for session_id in live:
            if self._clock() - began > REAP_PING_BUDGET_SECONDS:
                break
            self._call_quietly("ping", session_id)
        return len(stopped)

    def stop_all(self, budget: float = SHUTDOWN_BUDGET_SECONDS) -> None:
        """Stop every live stream on Plex, waiting at most *budget* seconds in all."""
        with self._lock:
            sessions = [s.session_id for s in self._streams.values()]
            self._streams.clear()
        threads = [
            threading.Thread(target=self._call_quietly, args=("stop", sid), daemon=True)
            for sid in sessions
        ]
        for thread in threads:
            thread.start()
        end = time.monotonic() + budget
        for thread in threads:
            thread.join(max(0.0, end - time.monotonic()))

    def tidy_orphans(self) -> int:
        """Stop Plex transcode sessions mediaman started but no longer tracks; return how many.

        Meant for startup, so Plex trouble is logged and reported as 0 rather than raised.
        """
        try:
            base, token = self._creds()
            raw = self._get(
                base,
                token,
                "/transcode/sessions",
                op="sessions",
                max_bytes=_PLAYLIST_MAX_BYTES,
                extra_headers={"Accept": "application/json"},
            )
            container = json.loads(raw).get("MediaContainer") or {}
            entries = container.get("TranscodeSession") or []
        except (PlexError, ValueError, AttributeError) as exc:
            logger.warning(
                "plex_stream: could not list transcode sessions (%s)", type(exc).__name__
            )
            return 0
        with self._lock:
            ours = {s.session_id for s in self._streams.values()} | self._issued
        orphans = []
        for entry in entries:
            key = str(entry.get("key", "")) if isinstance(entry, dict) else ""
            session_id = key.rsplit("/", 1)[-1]
            if session_id.startswith(SESSION_PREFIX) and session_id not in ours:
                orphans.append(session_id)
        for session_id in orphans:
            self._call_quietly("stop", session_id)
        return len(orphans)

    def _touch(self, stream_id: str, user: str) -> _Stream:
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.user != user:
                raise self._gone(stream_id, user)
            stream.last_access = self._clock()
            viewing = self._viewings.setdefault(
                (user, stream.rating_key), _Viewing(stream.viewing_started, stream.last_access)
            )
            viewing.touched = stream.last_access
            return stream

    def _creds(self) -> tuple[str, str]:
        creds = self._credentials()
        if creds is None or not creds[0] or not creds[1]:
            raise PlexNotConfigured("Plex is not set up")
        url, token = creds
        parts = urlsplit(url.strip())
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or "@" in parts.netloc
        ):
            raise PlexNotConfigured("the Plex address is not a valid http(s) address")
        return f"{parts.scheme.lower()}://{parts.netloc}{parts.path.rstrip('/')}", token

    def _open_session(
        self,
        base: str,
        token: str,
        session_id: str,
        rating_key: str,
        quality: str,
        burn_subtitles: bool,
        deadline: float,
    ) -> str:
        params = _session_params(session_id, rating_key, quality, burn_subtitles)
        session_header = {"X-Plex-Session-Identifier": session_id}
        # start.m3u8 answers 400 unless a decision for the same session came first.
        for action in ("decision", "start.m3u8"):
            body = self._get(
                base,
                token,
                f"{_TRANSCODE}/{action}",
                op=action,
                params=params,
                extra_headers=session_header,
                max_bytes=_PLAYLIST_MAX_BYTES,
                deadline=deadline,
            )
            if action == "decision" and quality == ORIGINAL and not _video_copied(body):
                raise _Unservable("Plex would not copy the video")
        variant = next(
            (
                line.strip()
                for line in body.decode("utf-8", "replace").splitlines()
                if line.strip() and not line.startswith("#")
            ),
            "",
        )
        if not _VARIANT_RE.fullmatch(variant):
            raise _Unservable("Plex sent a start playlist without a usable variant")
        child_path = f"{_TRANSCODE}/{variant}"
        # Plex answers 404 for every segment until the child playlist has been read once.
        playlist = self._get(
            base, token, child_path, op="playlist", max_bytes=_PLAYLIST_MAX_BYTES, deadline=deadline
        )
        if quality == ORIGINAL:
            # A copied video may be packaged in a way mediaman does not serve: know it now.
            _rewrite_playlist(playlist.decode("utf-8", "replace"), "")
        return child_path

    def _call_quietly(self, action: str, session_id: str) -> None:
        if action == "stop":
            with self._lock:
                self._issued.discard(session_id)
        try:
            base, token = self._creds()
            self._get(
                base,
                token,
                f"{_TRANSCODE}/{action}",
                op=action,
                params={"session": session_id},
                max_bytes=_PLAYLIST_MAX_BYTES,
                timeout=_QUIET_TIMEOUT,
            )
        except PlexError as exc:
            logger.warning("plex_stream: %s failed (%s)", action, exc)
        except Exception as exc:
            # Best effort: a failing cleanup must not replace the error that led to it.
            logger.warning("plex_stream: %s failed (%s)", action, type(exc).__name__)

    def _get(
        self,
        base: str,
        token: str,
        path: str,
        *,
        op: str,
        max_bytes: int,
        params: Mapping[str, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] = _TIMEOUT,
        deadline: float | None = None,
        check: Callable[[], None] = lambda: None,
    ) -> bytes:
        """GET *path* on Plex with the token in a header only, inside a DNS pin, body capped.

        With a *deadline* (on the manager's clock) the call is refused once it has passed, and
        its timeouts never run beyond it, nor the reading of the body. *check* runs before each
        chunk of the body and after a failure to reach Plex, and may raise to end the call.
        """
        if deadline is not None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise PlexUnavailable("Plex took too long")
            timeout = (min(timeout[0], remaining), min(timeout[1], remaining))
        url = base + path
        allowed = self._allowed_hosts() if callable(self._allowed_hosts) else self._allowed_hosts
        safe, hostname, pinned_ips = resolve_safe_outbound_url(url, allowed_hosts=allowed)
        if not safe:
            raise PlexUnavailable("the Plex address was refused by the outbound safety check")
        headers = {
            "X-Plex-Token": token,
            "X-Plex-Client-Identifier": _CLIENT_IDENTIFIER,
            "X-Plex-Product": "mediaman",
            "X-Plex-Version": __version__,
            "X-Plex-Platform": "Chrome",
            "User-Agent": _USER_AGENT,
            "Accept-Encoding": "identity",
            **(extra_headers or {}),
        }

        def go_on() -> None:
            check()
            if deadline is not None and self._clock() > deadline:
                raise PlexUnavailable("Plex took too long")

        ensure_hook_installed()
        pinned = pin(hostname, pinned_ips) if hostname and pinned_ips else contextlib.nullcontext()
        # No error raised here carries a requests exception: that names the URL, which holds the
        # Plex address and session id.
        try:
            with pinned:
                response = self._session.request(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    stream=True,
                    allow_redirects=False,
                    timeout=timeout,
                )
                try:
                    if response.status_code != 200:
                        logger.warning("plex_stream: %s answered HTTP %s", op, response.status_code)
                        raise PlexUnavailable(f"Plex answered HTTP {response.status_code}")
                    body = cast(requests.Response, _CheckedBody(response, go_on))
                    return _read_capped(body, max_bytes)
                finally:
                    response.close()
        except requests.RequestException as exc:
            failure = type(exc).__name__
        except (_SizeCapExceeded, _ContentTypeMismatch):
            logger.warning("plex_stream: %s got an oversized or encoded body", op)
            raise PlexUnavailable("Plex sent an unusable response") from None
        # Outside the handler, so that what is raised here does not hold the requests error.
        check()
        logger.warning("plex_stream: %s could not reach Plex (%s)", op, failure)
        raise PlexUnavailable("Plex could not be reached")


def _session_params(
    session_id: str, rating_key: str, quality: str, burn_subtitles: bool
) -> dict[str, str]:
    params = {
        "path": f"/library/metadata/{rating_key}",
        "protocol": "hls",
        "directPlay": "0",
        "directStream": "1",
        "fastSeek": "1",
        "mediaIndex": "0",
        "partIndex": "0",
        "offset": "0",
        "subtitles": "burn" if burn_subtitles else "none",
        "session": session_id,
    }
    if quality != ORIGINAL:
        resolution, bitrate = QUALITY_PRESETS[quality]
        params |= {"videoResolution": resolution, "maxVideoBitrate": str(bitrate)}
    return params


def _video_copied(body: bytes) -> bool:
    """True when a decision says the chosen version's video is copied, with nothing burned in."""
    try:
        root = ET.fromstring(body)
    except (ParseError, ValueError):
        return False
    media = list(root.iter("Media"))
    chosen = [m for m in media if m.get("selected") == "1"] or media
    streams = [s for m in chosen for s in m.iter("Stream")]
    video = [s for s in streams if s.get("streamType") == "1"]
    return (
        bool(video)
        and all(s.get("decision") == "copy" for s in video)
        and not any(s.get("decision") == "burn" for s in streams)
    )


def _rewrite_playlist(text: str, stream_id: str) -> str:
    """Rewrite a Plex media playlist so it names only mediaman segment routes.

    Any URI that is not a plain segment file name (absolute, rooted, traversing, with a query)
    rejects the whole playlist instead of being passed on.
    """
    lines = [line.strip() for line in text.splitlines()]
    if not lines or lines[0] != "#EXTM3U":
        raise _Unservable("Plex sent something that is not a playlist")
    out: list[str] = []
    for line in lines:
        if not line:
            continue
        if not line.startswith("#"):
            out.append(_segment_url(line, stream_id))
            continue
        if extinf := _EXTINF_RE.fullmatch(line):
            out.append(f"#EXTINF:{extinf.group(1)},")
            continue
        if line.startswith("#EXT-X-MAP"):
            map_tag = _MAP_RE.fullmatch(line)
            if map_tag is None:
                raise _Unservable("Plex sent a playlist mediaman cannot serve")
            out.append(f'#EXT-X-MAP:URI="{_segment_url(map_tag.group(1), stream_id)}"')
            continue
        if line == "#EXT-X-KEY:METHOD=NONE":
            out.append(line)
            continue
        if "URI=" in line:
            raise _Unservable("Plex sent a playlist mediaman cannot serve")
        tag, _, value = line.partition(":")
        if tag in _PLAIN_TAGS and _PLAIN_TAG_VALUE_RE.fullmatch(value):
            out.append(line)
    return "\n".join(out) + "\n"


def _segment_url(name: str, stream_id: str) -> str:
    if not _SEGMENT_NAME_RE.fullmatch(name):
        raise _Unservable("Plex sent a playlist mediaman cannot serve")
    return f"{SEGMENT_URL_PREFIX}/{stream_id}/seg/{name}"


_manager: StreamManager | None = None


def init_stream_manager(
    credentials: CredentialsProvider, *, allowed_hosts: AllowedHosts = None
) -> StreamManager:
    """Create the process-wide manager (replacing any earlier one) and return it."""
    global _manager
    _manager = StreamManager(credentials, allowed_hosts=allowed_hosts)
    return _manager


def get_stream_manager() -> StreamManager:
    """Return the process-wide manager; raises ``RuntimeError`` before ``init_stream_manager``."""
    if _manager is None:
        raise RuntimeError("stream manager is not initialised")
    return _manager
