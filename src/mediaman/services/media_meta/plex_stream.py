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
import json
import logging
import posixpath
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from mediaman import __version__
from mediaman.services.infra import resolve_safe_outbound_url
from mediaman.services.infra.http import ensure_hook_installed, pin
from mediaman.services.infra.http.streaming import (
    _ContentTypeMismatch,
    _read_capped,
    _SizeCapExceeded,
)
from mediaman.services.media_meta.plex_watch import (
    _RATING_KEY_RE,
    PlexError,
    PlexNotConfigured,
    PlexUnavailable,
)

logger = logging.getLogger(__name__)

#: ``(videoResolution, maxVideoBitrate kbps)``; Plex keeps the aspect ratio within the box.
QUALITY_PRESETS: dict[str, tuple[str, int]] = {
    "best": ("1920x1080", 8000),
    "medium": ("1280x720", 4000),
    "low": ("854x480", 1500),
}

SESSION_PREFIX = "mediaman-"
IDLE_TIMEOUT_SECONDS = 180.0
SEGMENT_URL_PREFIX = "/api/watch/stream"

_CLIENT_IDENTIFIER = "mediaman-watch"
_USER_AGENT = f"mediaman/{__version__}"
_TRANSCODE = "/video/:/transcode/universal"
_TIMEOUT: tuple[float, float] = (5.0, 30.0)
_SEGMENT_MAX_BYTES = 32 * 1024 * 1024
_PLAYLIST_MAX_BYTES = 4 * 1024 * 1024

# How long after a user last touched an item a new stream of it still counts as the same viewing,
# so the time the player has already played carries over a restart.
VIEWING_KEEP_SECONDS = 3600.0

MAX_STREAMS_PER_USER = 3
MAX_STREAMS_TOTAL = 20
_POOL_MAXSIZE = 32

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


class TooManyStreams(Exception):
    """The user, or mediaman as a whole, already has as many live streams as allowed."""


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
            adapter = HTTPAdapter(pool_maxsize=_POOL_MAXSIZE)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        self._session = session
        self._clock = clock
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}
        # Plex session ids handed out and not yet stopped, including ones whose Plex session is
        # still opening and so not yet in ``_streams``; the startup tidy must leave them alone.
        self._issued: set[str] = set()
        # (user, rating_key) -> (when the viewing began, when it was last touched).
        self._viewings: dict[tuple[str, str], tuple[float, float]] = {}

    def start(
        self,
        user: str,
        rating_key: str,
        quality: str,
        burn_subtitles: bool,
        info: Mapping[str, Any] | None = None,
    ) -> str:
        """Open a Plex transcode session at offset 0 and return the new stream id.

        A failed start is retried once with a fresh session, since Plex intermittently refuses
        a start right after another one for the same item. The user's other streams of the same
        item are stopped first, since the player starts a new one on a quality change or restart.
        Raises ``ValueError`` for an unknown quality or a malformed rating key, and
        ``TooManyStreams`` when the user, or mediaman as a whole, is at the limit.
        """
        if quality not in QUALITY_PRESETS:
            raise ValueError(f"unknown quality {quality!r}")
        if not _RATING_KEY_RE.fullmatch(rating_key):
            raise ValueError("rating key must be numeric")
        base, token = self._creds()
        for session_id in self._drop_same_item(user, rating_key):
            self._call_quietly("stop", session_id)
        with self._lock:
            self._check_room(user)
        for attempt in range(2):
            session_id = SESSION_PREFIX + secrets.token_urlsafe(16)
            with self._lock:
                self._issued.add(session_id)
            try:
                child_path = self._open_session(
                    base, token, session_id, rating_key, quality, burn_subtitles
                )
                break
            except PlexUnavailable:
                self._call_quietly("stop", session_id)
                if attempt == 1:
                    raise
                logger.info("plex_stream: start failed, retrying once")
        stream_id = secrets.token_urlsafe(16)
        with self._lock:
            # A concurrent start of the same item may have stored its stream meanwhile.
            twins = [
                sid
                for sid, s in self._streams.items()
                if s.user == user and s.rating_key == rating_key
            ]
            replaced = [self._streams.pop(sid).session_id for sid in twins]
            try:
                self._check_room(user)
            except TooManyStreams:
                full = True
            else:
                full = False
                now = self._clock()
                self._viewings = {
                    k: v for k, v in self._viewings.items() if now - v[1] <= VIEWING_KEEP_SECONDS
                }
                began = self._viewings.get((user, rating_key), (now, now))[0]
                self._viewings[(user, rating_key)] = (began, now)
                self._streams[stream_id] = _Stream(
                    user=user,
                    rating_key=rating_key,
                    quality=quality,
                    session_id=session_id,
                    child_path=child_path,
                    last_access=now,
                    viewing_started=began,
                    info=dict(info or {}),
                )
        for old_session in replaced:
            self._call_quietly("stop", old_session)
        if full:
            # Concurrent starts can all pass the first check; the one that loses gives its session back.
            self._call_quietly("stop", session_id)
            raise TooManyStreams("too many streams are open")
        return stream_id

    def _drop_same_item(self, user: str, rating_key: str) -> list[str]:
        with self._lock:
            ids = [
                sid
                for sid, s in self._streams.items()
                if s.user == user and s.rating_key == rating_key
            ]
            return [self._streams.pop(sid).session_id for sid in ids]

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

    def segment(self, stream_id: str, user: str, name: str) -> tuple[bytes, str]:
        """Fetch one segment whole and return ``(body, content_type)``.

        Raises ``ValueError`` for a name that is not a plain segment file name.
        """
        if not _SEGMENT_NAME_RE.fullmatch(name):
            raise ValueError("not a segment name")
        stream = self._touch(stream_id, user)
        base, token = self._creds()
        path = f"{posixpath.dirname(stream.child_path)}/{name}"
        body = self._get(base, token, path, op="segment", max_bytes=_SEGMENT_MAX_BYTES)
        return body, _CONTENT_TYPES[name.rsplit(".", 1)[1]]

    def get(self, stream_id: str, user: str) -> StreamInfo:
        stream = self._touch(stream_id, user)
        return StreamInfo(
            rating_key=stream.rating_key,
            quality=stream.quality,
            info=dict(stream.info),
            elapsed_s=self._clock() - stream.viewing_started,
        )

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
            live = [s.session_id for s in self._streams.values()]
        for stream in stopped:
            self._call_quietly("stop", stream.session_id)
        for session_id in live:
            self._call_quietly("ping", session_id)
        return len(stopped)

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
                raise StreamGone("stream is gone")
            stream.last_access = self._clock()
            self._viewings[(user, stream.rating_key)] = (stream.viewing_started, stream.last_access)
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
    ) -> str:
        resolution, bitrate = QUALITY_PRESETS[quality]
        params = {
            "path": f"/library/metadata/{rating_key}",
            "protocol": "hls",
            "directPlay": "0",
            "directStream": "1",
            "fastSeek": "1",
            "mediaIndex": "0",
            "partIndex": "0",
            "offset": "0",
            "videoResolution": resolution,
            "maxVideoBitrate": str(bitrate),
            "subtitles": "burn" if burn_subtitles else "none",
            "session": session_id,
        }
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
            )
        variant = next(
            (
                line.strip()
                for line in body.decode("utf-8", "replace").splitlines()
                if line.strip() and not line.startswith("#")
            ),
            "",
        )
        if not _VARIANT_RE.fullmatch(variant):
            raise PlexUnavailable("Plex sent a start playlist without a usable variant")
        child_path = f"{_TRANSCODE}/{variant}"
        # Plex answers 404 for every segment until the child playlist has been read once.
        self._get(base, token, child_path, op="playlist", max_bytes=_PLAYLIST_MAX_BYTES)
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
            )
        except PlexError as exc:
            logger.warning("plex_stream: %s failed (%s)", action, exc)

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
    ) -> bytes:
        """GET *path* on Plex with the token in a header only, inside a DNS pin, body capped."""
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
        ensure_hook_installed()
        pinned = pin(hostname, pinned_ips) if hostname and pinned_ips else contextlib.nullcontext()
        # Errors are raised "from None": a requests exception names the URL, which holds the
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
                    timeout=_TIMEOUT,
                )
                try:
                    if response.status_code != 200:
                        logger.warning("plex_stream: %s answered HTTP %s", op, response.status_code)
                        raise PlexUnavailable(f"Plex answered HTTP {response.status_code}")
                    return _read_capped(response, max_bytes)
                finally:
                    response.close()
        except requests.RequestException as exc:
            logger.warning("plex_stream: %s could not reach Plex (%s)", op, type(exc).__name__)
            raise PlexUnavailable("Plex could not be reached") from None
        except (_SizeCapExceeded, _ContentTypeMismatch):
            logger.warning("plex_stream: %s got an oversized or encoded body", op)
            raise PlexUnavailable("Plex sent an unusable response") from None


def _rewrite_playlist(text: str, stream_id: str) -> str:
    """Rewrite a Plex media playlist so it names only mediaman segment routes.

    Any URI that is not a plain segment file name (absolute, rooted, traversing, with a query)
    rejects the whole playlist instead of being passed on.
    """
    lines = [line.strip() for line in text.splitlines()]
    if not lines or lines[0] != "#EXTM3U":
        raise PlexUnavailable("Plex sent something that is not a playlist")
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
                raise PlexUnavailable("Plex sent a playlist mediaman cannot serve")
            out.append(f'#EXT-X-MAP:URI="{_segment_url(map_tag.group(1), stream_id)}"')
            continue
        if line == "#EXT-X-KEY:METHOD=NONE":
            out.append(line)
            continue
        if "URI=" in line:
            raise PlexUnavailable("Plex sent a playlist mediaman cannot serve")
        tag, _, value = line.partition(":")
        if tag in _PLAIN_TAGS and _PLAIN_TAG_VALUE_RE.fullmatch(value):
            out.append(line)
    return "\n".join(out) + "\n"


def _segment_url(name: str, stream_id: str) -> str:
    if not _SEGMENT_NAME_RE.fullmatch(name):
        raise PlexUnavailable("Plex sent a playlist mediaman cannot serve")
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
