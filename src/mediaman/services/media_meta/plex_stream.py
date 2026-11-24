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
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as ET
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
# How long a reaped stream is remembered, so a stop or progress beacon that lost the race with
# the reaper still saves the position.
REAPED_KEEP_SECONDS = 600.0
# How long Plex's word on whether it would copy a file's video is kept.
COPY_VERDICT_KEEP_SECONDS = 3600.0

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
    #: The viewing's played time as last saved.
    saved_played_s: float = 0.0


@dataclass
class _Viewing:
    """One user's viewing of one item, across restarts of its stream."""

    began: float
    touched: float
    saved_played_s: float = 0.0


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
        # (user, rating_key) -> the viewing.
        self._viewings: dict[tuple[str, str], _Viewing] = {}
        # stream id -> (when it was reaped, the stream), kept for REAPED_KEEP_SECONDS.
        self._reaped: dict[str, tuple[float, _Stream]] = {}
        # (rating_key, file) -> (when Plex was asked, whether it would copy the video).
        self._copies: dict[tuple[str, str], tuple[float, bool]] = {}

    def start(
        self,
        user: str,
        rating_key: str,
        quality: str,
        burn_subtitles: bool,
        info: Mapping[str, Any] | None = None,
        *,
        fallback: str | None = None,
    ) -> str:
        """Open a Plex transcode session at offset 0 and return the new stream id.

        A failed start is retried once with a fresh session, since Plex intermittently refuses
        a start right after another one for the same item. An original that Plex would not copy
        or could not start is retried as the *fallback* quality. The user's other streams of the
        same item are stopped first, since the player starts a new one on a quality change or
        restart.
        Raises ``ValueError`` for an unknown quality or a malformed rating key, and
        ``TooManyStreams`` when the user, or mediaman as a whole, is at the limit.
        """
        if quality not in QUALITY_PRESETS and quality != ORIGINAL:
            raise ValueError(f"unknown quality {quality!r}")
        if not _RATING_KEY_RE.fullmatch(rating_key):
            raise ValueError("rating key must be numeric")
        base, token = self._creds()
        for session_id in self._drop_same_item(user, rating_key):
            self._call_quietly("stop", session_id)
        with self._lock:
            self._check_room(user)
        deadline = self._clock() + START_DEADLINE_SECONDS
        for attempt in range(2):
            session_id = SESSION_PREFIX + secrets.token_urlsafe(16)
            with self._lock:
                self._issued.add(session_id)
            try:
                child_path = self._open_session(
                    base, token, session_id, rating_key, quality, burn_subtitles, deadline
                )
                break
            except PlexUnavailable:
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
                    k: v
                    for k, v in self._viewings.items()
                    if now - v.touched <= VIEWING_KEEP_SECONDS
                }
                viewing = self._viewings.setdefault((user, rating_key), _Viewing(now, now))
                viewing.touched = now
                self._streams[stream_id] = _Stream(
                    user=user,
                    rating_key=rating_key,
                    quality=quality,
                    session_id=session_id,
                    child_path=child_path,
                    last_access=now,
                    viewing_started=viewing.began,
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
        )

    def note_played(self, user: str, rating_key: str, played_s: float) -> None:
        """Remember the played time just saved for *user*'s viewing of the item."""
        with self._lock:
            viewing = self._viewings.get((user, rating_key))
            if viewing is not None:
                viewing.saved_played_s = played_s

    def peek(self, stream_id: str, user: str) -> StreamInfo:
        """As ``get``, without counting as use of the stream, so an idle one still ends on time."""
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.user != user:
                raise StreamGone("stream is gone")
            return self._info(stream, stream.last_access)

    def copies_video(self, rating_key: str, file: str) -> bool:
        """Whether Plex would send this item's video as it is, remembered per *file* for a while.

        Only asks Plex for its decision, which opens nothing. False when Plex cannot say.
        """
        if not _RATING_KEY_RE.fullmatch(rating_key):
            return False
        now = self._clock()
        with self._lock:
            asked_at, verdict = self._copies.get((rating_key, file), (0.0, None))
        if verdict is not None and now - asked_at <= COPY_VERDICT_KEEP_SECONDS:
            return verdict
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
            return False
        verdict = _video_copied(body)
        with self._lock:
            self._copies = {
                k: v for k, v in self._copies.items() if now - v[0] <= COPY_VERDICT_KEEP_SECONDS
            }
            self._copies[(rating_key, file)] = (now, verdict)
        return verdict

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
                if now - kept[0] <= REAPED_KEEP_SECONDS
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
                raise StreamGone("stream is gone")
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
                raise PlexUnavailable("Plex would not copy the video")
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
    ) -> bytes:
        """GET *path* on Plex with the token in a header only, inside a DNS pin, body capped.

        With a *deadline* (on the manager's clock) the call is refused once it has passed, and
        its timeouts never run beyond it.
        """
        if deadline is not None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise PlexUnavailable("Plex took too long to start the stream")
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
                    timeout=timeout,
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
