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

from mediaman import __version__
from mediaman.services.infra import resolve_safe_outbound_url
from mediaman.services.infra.http import ensure_hook_installed, pin
from mediaman.services.infra.http.streaming import (
    _ContentTypeMismatch,
    _read_capped,
    _SizeCapExceeded,
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

_RATING_KEY_RE = re.compile(r"[0-9]{1,12}")
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


class PlexUnavailable(Exception):
    """Plex could not be reached or answered with something unusable."""


class PlexNotConfigured(PlexUnavailable):
    """No usable Plex address and token are set."""


#: Returns ``(plex_url, plex_token)``, or ``None`` when Plex is not set up.
CredentialsProvider = Callable[[], tuple[str, str] | None]


@dataclass(frozen=True)
class StreamInfo:
    rating_key: str
    quality: str
    info: Mapping[str, Any]


@dataclass
class _Stream:
    user: str
    rating_key: str
    quality: str
    session_id: str
    child_path: str
    last_access: float
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
    ) -> None:
        self._credentials = credentials
        self._session = session if session is not None else requests.Session()
        self._clock = clock
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}

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
        a start right after another one for the same item. Raises ``ValueError`` for an unknown
        quality or a malformed rating key.
        """
        if quality not in QUALITY_PRESETS:
            raise ValueError(f"unknown quality {quality!r}")
        if not _RATING_KEY_RE.fullmatch(rating_key):
            raise ValueError("rating key must be numeric")
        base, token = self._creds()
        for attempt in range(2):
            session_id = SESSION_PREFIX + secrets.token_urlsafe(16)
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
            self._streams[stream_id] = _Stream(
                user=user,
                rating_key=rating_key,
                quality=quality,
                session_id=session_id,
                child_path=child_path,
                last_access=self._clock(),
                info=dict(info or {}),
            )
        return stream_id

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
            rating_key=stream.rating_key, quality=stream.quality, info=dict(stream.info)
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
        except (PlexUnavailable, ValueError, AttributeError) as exc:
            logger.warning(
                "plex_stream: could not list transcode sessions (%s)", type(exc).__name__
            )
            return 0
        with self._lock:
            ours = {s.session_id for s in self._streams.values()}
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
        return f"{parts.scheme.lower()}://{parts.netloc}", token

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
        except PlexUnavailable as exc:
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
        safe, hostname, pinned_ips = resolve_safe_outbound_url(url)
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


def init_stream_manager(credentials: CredentialsProvider) -> StreamManager:
    """Create the process-wide manager (replacing any earlier one) and return it."""
    global _manager
    _manager = StreamManager(credentials)
    return _manager


def get_stream_manager() -> StreamManager:
    """Return the process-wide manager; raises ``RuntimeError`` before ``init_stream_manager``."""
    if _manager is None:
        raise RuntimeError("stream manager is not initialised")
    return _manager
