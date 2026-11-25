"""Tests for the Plex stream proxy service, against a fake Plex HTTP layer."""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import ssl
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mediaman.services.media_meta import plex_stream
from mediaman.services.media_meta.plex_stream import (
    MAX_READS_PER_STREAM,
    MAX_READS_PER_USER,
    MAX_READS_TOTAL,
    MAX_STARTS_PER_USER,
    MAX_STARTS_TOTAL,
    MAX_STREAMS_PER_USER,
    MAX_STREAMS_TOTAL,
    SegmentRead,
    StreamGone,
    StreamManager,
    StreamReplaced,
    TooManyReads,
    TooManyStarts,
    TooManyStreams,
    init_stream_manager,
)
from mediaman.services.media_meta.plex_watch import PlexError, PlexNotConfigured, PlexUnavailable

PLEX_URL = "http://plex.test:32400"
TOKEN = "SECRET-TOKEN-123"
TRANSCODE = "/video/:/transcode/universal"

CHILD_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:8\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n"
    "#EXT-X-PLAYLIST-TYPE:VOD\n"
    f"# served by {PLEX_URL}/?X-Plex-Token={TOKEN}\n"
    '#EXT-X-SESSION-DATA:DATA-ID="x",VALUE="http://plex.test:32400/"\n'
    "#EXTINF:8.000000,plex.test title\n"
    "00000.ts\n"
    "#EXTINF:3.5,\n"
    "00001.ts\n"
    "#EXT-X-ENDLIST\n"
)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status_code = status
        self.headers: dict[str, str] = {}
        self._body = body

    def iter_content(self, chunk_size: int = 65536):
        yield self._body

    def close(self) -> None:
        pass


class FakePlex:
    """Plex's transcoder as observed: start needs a prior decision for the same session, and
    segments answer 404 until the child playlist has been read."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.decided: set[str] = set()
        self.child_read: set[str] = set()
        self.fail_decisions = 0
        self.decision = b"<MediaContainer/>"
        self.child_playlist = CHILD_PLAYLIST
        self.sessions: list[dict] = []
        self.unreachable = False
        self.variant = None
        self.prefix = ""
        self.on_start = None
        self.on_call = None

    def request(self, method, url, *, params=None, headers=None, **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers),
                "timeout": kwargs.get("timeout"),
            }
        )
        if self.on_call:
            self.on_call(urlsplit(url).path)
        if self.unreachable:
            raise requests.ConnectionError(f"cannot connect to {url}?X-Plex-Token={TOKEN}")
        path = urlsplit(url).path
        if not path.startswith(self.prefix):
            return FakeResponse(404)
        path = path[len(self.prefix) :]
        session = (params or {}).get("session")
        if path == f"{TRANSCODE}/decision":
            if self.fail_decisions:
                self.fail_decisions -= 1
                return FakeResponse(500)
            self.decided.add(session)
            return FakeResponse(200, self.decision)
        if path == f"{TRANSCODE}/start.m3u8":
            if self.on_start:
                self.on_start(session)
            if session not in self.decided:
                return FakeResponse(400)
            variant = self.variant or f"session/{session}/base/index.m3u8"
            master = f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=8000000\n{variant}\n"
            return FakeResponse(200, master.encode())
        if path in (f"{TRANSCODE}/ping", f"{TRANSCODE}/stop"):
            return FakeResponse(200)
        if path == "/transcode/sessions":
            return FakeResponse(
                200, json.dumps({"MediaContainer": {"TranscodeSession": self.sessions}}).encode()
            )
        prefix = f"{TRANSCODE}/session/"
        if path.startswith(prefix):
            sid, _, name = path[len(prefix) :].partition("/base/")
            if name == "index.m3u8":
                self.child_read.add(sid)
                return FakeResponse(200, self.child_playlist.encode())
            if sid not in self.child_read:
                return FakeResponse(404)
            return FakeResponse(200, f"segment {name}".encode())
        return FakeResponse(404)

    def paths(self) -> list[str]:
        return [urlsplit(c["url"]).path for c in self.calls]

    def calls_to(self, action: str) -> list[dict]:
        return [c for c in self.calls if urlsplit(c["url"]).path == f"{TRANSCODE}/{action}"]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _safe_resolve(monkeypatch):
    monkeypatch.setattr(
        plex_stream,
        "resolve_safe_outbound_url",
        lambda url, **kwargs: (True, "plex.test", ("203.0.113.5",)),
    )


@pytest.fixture
def plex() -> FakePlex:
    return FakePlex()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def manager(plex, clock) -> StreamManager:
    return StreamManager(lambda: (PLEX_URL, TOKEN), session=plex, clock=clock)


def _start(manager: StreamManager, user: str = "alice", **kwargs) -> str:
    args = {"rating_key": "123", "quality": "best", "burn_subtitles": False} | kwargs
    return manager.start(user, **args)


def _segment(manager: StreamManager, stream_id: str, user: str, name: str) -> tuple[bytes, str]:
    read = SegmentRead(stream_id, user)
    manager.claim_read(read)
    try:
        return manager.segment(read, name)
    finally:
        manager.release_read(read)


def test_decision_comes_before_start_with_the_same_session(manager, plex):
    _start(manager)

    assert plex.paths()[:2] == [f"{TRANSCODE}/decision", f"{TRANSCODE}/start.m3u8"]
    decision, start = plex.calls[0], plex.calls[1]
    session = decision["params"]["session"]
    assert session.startswith("mediaman-")
    assert start["params"]["session"] == session
    assert decision["headers"]["X-Plex-Session-Identifier"] == session
    assert start["headers"]["X-Plex-Session-Identifier"] == session


def test_child_playlist_is_read_before_any_segment(manager, plex):
    stream_id = _start(manager)

    body, content_type = _segment(manager, stream_id, "alice", "00001.ts")

    assert body == b"segment 00001.ts"
    assert content_type == "video/mp2t"
    paths = plex.paths()
    child = next(i for i, p in enumerate(paths) if p.endswith("/index.m3u8"))
    first_segment = next(i for i, p in enumerate(paths) if p.endswith(".ts"))
    assert child < first_segment


def test_rewritten_playlist_names_only_mediaman_segment_routes(manager):
    stream_id = _start(manager)

    text = manager.playlist(stream_id, "alice")

    assert "plex.test" not in text
    assert TOKEN not in text
    assert "session" not in text
    uris = [line for line in text.splitlines() if not line.startswith("#")]
    assert uris == [
        f"/api/watch/stream/{stream_id}/seg/00000.ts",
        f"/api/watch/stream/{stream_id}/seg/00001.ts",
    ]
    assert "#EXTINF:8.000000," in text
    assert "#EXTINF:3.5," in text
    assert text.splitlines()[-1] == "#EXT-X-ENDLIST"


@pytest.mark.parametrize(
    "bad_line",
    [
        f"{PLEX_URL}{TRANSCODE}/session/x/base/00002.ts",
        f"{TRANSCODE}/session/x/base/00002.ts",
        "../00002.ts",
        "base/00002.ts",
        f"00002.ts?X-Plex-Token={TOKEN}",
        f'#EXT-X-MAP:URI="{PLEX_URL}/init.mp4"',
        f'#EXT-X-KEY:METHOD=AES-128,URI="{PLEX_URL}/key"',
    ],
)
def test_playlist_with_an_unservable_uri_is_refused_without_leaking_it(manager, plex, bad_line):
    stream_id = _start(manager)
    plex.child_playlist = CHILD_PLAYLIST.replace("#EXT-X-ENDLIST", f"{bad_line}\n#EXT-X-ENDLIST")

    with pytest.raises(PlexUnavailable) as excinfo:
        manager.playlist(stream_id, "alice")

    assert "plex.test" not in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


@pytest.mark.parametrize(
    "name",
    ["../index.m3u8", "00001.ts/../../x", "index.m3u8", "1234567.ts", "00001.ts\n", "00001.exe"],
)
def test_segment_name_that_is_not_a_plain_segment_is_refused_before_plex(manager, plex, name):
    stream_id = _start(manager)
    calls_before = len(plex.calls)

    with pytest.raises(ValueError):
        _segment(manager, stream_id, "alice", name)

    assert len(plex.calls) == calls_before


def test_another_users_stream_is_indistinguishable_from_an_unknown_one(manager):
    stream_id = _start(manager, user="alice")

    with pytest.raises(StreamGone) as unknown:
        manager.playlist("no-such-stream", "bob")
    for attempt in (
        lambda: manager.playlist(stream_id, "bob"),
        lambda: _segment(manager, stream_id, "bob", "00000.ts"),
        lambda: manager.get(stream_id, "bob"),
        lambda: manager.stop(stream_id, "bob"),
    ):
        with pytest.raises(StreamGone) as other:
            attempt()
        assert str(other.value) == str(unknown.value)

    assert manager.get(stream_id, "alice").rating_key == "123"


def test_get_returns_what_the_caller_attached(manager):
    stream_id = _start(manager, info={"duration_s": 5400.0, "media_item_id": "77"})

    stream = manager.get(stream_id, "alice")

    assert stream.info == {"duration_s": 5400.0, "media_item_id": "77"}
    assert stream.quality == "best"


def test_stop_stops_the_plex_session_and_forgets_the_stream(manager, plex):
    stream_id = _start(manager)
    session = plex.calls[0]["params"]["session"]

    manager.stop(stream_id, "alice")

    assert [c["params"]["session"] for c in plex.calls_to("stop")] == [session]
    with pytest.raises(StreamGone):
        manager.playlist(stream_id, "alice")


def test_reap_stops_idle_streams_on_plex_and_keeps_active_ones(manager, plex, clock):
    idle = _start(manager)
    idle_session = plex.calls[0]["params"]["session"]
    clock.now += 100
    active = _start(manager, rating_key="456")
    active_session = plex.calls_to("decision")[-1]["params"]["session"]
    clock.now += 81

    assert manager.reap(clock.now) == 1

    assert [c["params"]["session"] for c in plex.calls_to("stop")] == [idle_session]
    assert [c["params"]["session"] for c in plex.calls_to("ping")] == [active_session]
    with pytest.raises(StreamGone):
        manager.playlist(idle, "alice")
    manager.playlist(active, "alice")


def test_access_keeps_a_stream_from_being_reaped(manager, clock):
    stream_id = _start(manager)
    clock.now += 170
    _segment(manager, stream_id, "alice", "00000.ts")
    clock.now += 170

    assert manager.reap(clock.now) == 0


def test_a_failed_start_is_retried_once_with_a_fresh_session(manager, plex):
    plex.fail_decisions = 1

    stream_id = _start(manager)

    sessions = [c["params"]["session"] for c in plex.calls_to("decision")]
    assert len(sessions) == 2
    assert sessions[0] != sessions[1]
    assert [c["params"]["session"] for c in plex.calls_to("stop")] == [sessions[0]]
    _segment(manager, stream_id, "alice", "00000.ts")


def test_start_gives_up_after_the_retry(manager, plex):
    plex.fail_decisions = 2

    with pytest.raises(PlexUnavailable):
        _start(manager)

    assert len(plex.calls_to("decision")) == 2


@pytest.mark.parametrize(
    ("quality", "resolution", "bitrate"),
    [("best", "1920x1080", "8000"), ("medium", "1280x720", "4000"), ("low", "854x480", "1500")],
)
def test_quality_preset_sets_resolution_and_bitrate(manager, plex, quality, resolution, bitrate):
    _start(manager, quality=quality)

    for call in plex.calls_to("decision") + plex.calls_to("start.m3u8"):
        assert call["params"]["videoResolution"] == resolution
        assert call["params"]["maxVideoBitrate"] == bitrate
        assert call["params"]["offset"] == "0"
        assert call["params"]["path"] == "/library/metadata/123"


@pytest.mark.parametrize(("burn", "expected"), [(True, "burn"), (False, "none")])
def test_subtitles_are_burned_only_when_asked(manager, plex, burn, expected):
    _start(manager, burn_subtitles=burn)

    assert plex.calls_to("start.m3u8")[0]["params"]["subtitles"] == expected


@pytest.mark.parametrize(("quality", "rating_key"), [("ultra", "123"), ("best", "1&path=/x")])
def test_start_refuses_unknown_quality_or_malformed_key(manager, plex, quality, rating_key):
    with pytest.raises(ValueError):
        manager.start("alice", rating_key, quality, False)

    assert plex.calls == []


def test_tidy_orphans_stops_only_mediaman_sessions(manager, plex):
    plex.sessions = [
        {"key": "mediaman-abc"},
        {"key": "/transcode/sessions/mediaman-def"},
        {"key": "plexweb-xyz"},
        {"key": "/transcode/sessions/tv-mediaman-ghi"},
    ]

    assert manager.tidy_orphans() == 2

    stopped = [c["params"]["session"] for c in plex.calls_to("stop")]
    assert stopped == ["mediaman-abc", "mediaman-def"]


def test_tidy_leaves_a_session_that_is_still_being_opened(manager, plex):
    tidied: list[int] = []

    def tidy_while_opening(session: str) -> None:
        plex.sessions = [{"key": session}]
        tidied.append(manager.tidy_orphans())

    plex.on_start = tidy_while_opening
    _start(manager)

    assert tidied == [0]
    assert plex.calls_to("stop") == []


def test_viewing_time_carries_across_a_restart_of_the_stream(manager, clock):
    first = _start(manager)
    clock.now += 280
    manager.get(first, "alice")
    second = _start(manager)  # the player restarts at its position
    clock.now += 20

    assert manager.get(second, "alice").elapsed_s == 300


def test_viewing_time_starts_over_after_a_long_gap(manager, clock):
    _start(manager)
    clock.now += plex_stream.VIEWING_KEEP_SECONDS + 1
    second = _start(manager)
    clock.now += 5

    assert manager.get(second, "alice").elapsed_s == 5


def test_tidy_orphans_reports_zero_when_plex_is_down(manager, plex):
    plex.unreachable = True

    assert manager.tidy_orphans() == 0


def test_whole_lifecycle_writes_nothing_and_sends_the_token_only_in_a_header(manager, plex, clock):
    plex.sessions = [{"key": "mediaman-old"}]
    stream_id = _start(manager)
    manager.playlist(stream_id, "alice")
    _segment(manager, stream_id, "alice", "00001.ts")
    manager.get(stream_id, "alice")
    manager.reap(clock.now)
    manager.tidy_orphans()
    manager.stop(stream_id, "alice")

    for call in plex.calls:
        assert call["method"] == "GET"
        path = urlsplit(call["url"]).path
        assert "/:/timeline" not in path
        assert "/:/scrobble" not in path
        assert "/library/parts" not in path
        assert TOKEN not in call["url"]
        assert TOKEN not in json.dumps(call["params"])
        assert call["headers"]["X-Plex-Token"] == TOKEN
        assert call["headers"]["User-Agent"].startswith("mediaman/")
        assert call["headers"]["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("failure", ["unreachable", "bad_status"])
def test_plex_failure_leaks_no_address_or_token(manager, plex, caplog, failure):
    stream_id = _start(manager)
    if failure == "unreachable":
        plex.unreachable = True
    else:
        plex.request = lambda *a, **k: FakeResponse(503)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(PlexUnavailable) as excinfo:
        manager.playlist(stream_id, "alice")
    manager.stop(stream_id, "alice")

    for text in (str(excinfo.value), repr(excinfo.value), caplog.text):
        assert "plex.test" not in text
        assert TOKEN not in text
    error = excinfo.value
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_refused_plex_address_sends_nothing(manager, plex, monkeypatch):
    monkeypatch.setattr(
        plex_stream, "resolve_safe_outbound_url", lambda url, **kwargs: (False, None, None)
    )

    with pytest.raises(PlexUnavailable):
        _start(manager)

    assert plex.calls == []


@pytest.mark.parametrize("creds", [None, ("", TOKEN), (PLEX_URL, ""), ("ftp://plex.test", TOKEN)])
def test_missing_or_bad_credentials_mean_not_configured(plex, creds):
    manager = StreamManager(lambda: creds, session=plex)

    with pytest.raises(PlexNotConfigured):
        _start(manager)

    assert plex.calls == []


def test_a_user_cannot_hold_more_than_the_per_user_limit(manager, plex):
    for key in range(MAX_STREAMS_PER_USER):
        _start(manager, rating_key=str(key + 1))
    starts = len(plex.calls_to("start.m3u8"))

    with pytest.raises(TooManyStreams):
        _start(manager, rating_key="99")

    assert len(plex.calls_to("start.m3u8")) == starts
    _start(manager, user="bob", rating_key="99")


def test_the_limit_frees_up_when_a_stream_is_stopped(manager):
    ids = [_start(manager, rating_key=str(k + 1)) for k in range(MAX_STREAMS_PER_USER)]

    manager.stop(ids[0], "alice")

    _start(manager, rating_key="99")


def test_restarting_the_same_item_replaces_the_old_stream_and_stops_it_on_plex(manager, plex):
    first = _start(manager)
    first_session = plex.calls_to("start.m3u8")[0]["params"]["session"]

    for _ in range(MAX_STREAMS_PER_USER + 2):
        second = _start(manager, quality="low")

    with pytest.raises(StreamGone):
        manager.get(first, "alice")
    assert manager.get(second, "alice").quality == "low"
    stopped = [c["params"]["session"] for c in plex.calls_to("stop")]
    assert first_session in stopped


def test_total_streams_are_capped_across_users(manager):
    for n in range(MAX_STREAMS_TOTAL):
        _start(manager, user=f"user{n}")

    with pytest.raises(TooManyStreams):
        _start(manager, user="one-more")


def test_a_start_that_loses_a_race_for_the_last_slot_gives_its_session_back(manager, plex):
    for key in range(MAX_STREAMS_PER_USER - 1):
        _start(manager, rating_key=str(key + 1))
    real_open = manager._open_session
    raced = []

    def open_then_other_start_wins(*args):
        child = real_open(*args)
        if not raced:
            raced.append(True)
            _start(manager, rating_key="77")
        return child

    manager._open_session = open_then_other_start_wins
    loser_index = len(plex.calls_to("start.m3u8"))

    with pytest.raises(TooManyStreams):
        _start(manager, rating_key="88")

    last_stop = plex.calls_to("stop")[-1]["params"]["session"]
    assert last_stop == plex.calls_to("start.m3u8")[loser_index]["params"]["session"]


def test_of_two_starts_of_the_same_item_racing_the_later_plays_and_the_other_is_stopped(
    manager, plex
):
    real_open = manager._open_session
    raced = []
    inner = []

    def open_then_same_item_starts(*args):
        child = real_open(*args)
        if not raced:
            raced.append(True)
            inner.append(_start(manager))
        return child

    manager._open_session = open_then_same_item_starts

    outer = _start(manager)

    assert len(manager._streams) == 1
    assert manager.get(inner[0], "alice")
    with pytest.raises(StreamReplaced):
        manager.get(outer, "alice")
    sessions = [c["params"]["session"] for c in plex.calls_to("start.m3u8")]
    stopped = [c["params"]["session"] for c in plex.calls_to("stop")]
    assert stopped == [sessions[0]]


def test_plex_served_under_a_path_prefix_keeps_the_prefix(plex):
    plex.prefix = "/plex"
    manager = StreamManager(lambda: (f"{PLEX_URL}/plex/", TOKEN), session=plex)

    stream_id = _start(manager)
    manager.playlist(stream_id, "alice")
    _segment(manager, stream_id, "alice", "00000.ts")
    manager.stop(stream_id, "alice")

    assert plex.calls
    assert all(urlsplit(c["url"]).path.startswith("/plex/video/") for c in plex.calls)


@pytest.mark.parametrize("hosts", [frozenset({"plex.test"}), lambda: frozenset({"plex.test"})])
def test_the_allowed_hosts_reach_the_outbound_safety_check(plex, monkeypatch, hosts):
    seen = []

    def resolve(url, **kwargs):
        seen.append(kwargs.get("allowed_hosts"))
        return True, "plex.test", ("203.0.113.5",)

    monkeypatch.setattr(plex_stream, "resolve_safe_outbound_url", resolve)
    manager = StreamManager(lambda: (PLEX_URL, TOKEN), session=plex, allowed_hosts=hosts)

    _start(manager)

    assert seen
    assert all(h == frozenset({"plex.test"}) for h in seen)


def test_init_stream_manager_passes_the_allowed_hosts_on(monkeypatch):
    monkeypatch.setattr(plex_stream, "_manager", None)
    manager = init_stream_manager(lambda: None, allowed_hosts=frozenset({"plex.test"}))

    assert manager._allowed_hosts == frozenset({"plex.test"})


def test_stream_errors_are_the_plex_watch_errors(plex):
    manager = StreamManager(lambda: None, session=plex)

    with pytest.raises(PlexNotConfigured) as excinfo:
        _start(manager)

    assert isinstance(excinfo.value, PlexError)
    assert PlexUnavailable is plex_stream.PlexUnavailable


def test_a_rating_key_of_eighteen_digits_is_accepted(manager, plex):
    _start(manager, rating_key="1" * 18)

    assert plex.calls_to("start.m3u8")[0]["params"]["path"] == f"/library/metadata/{'1' * 18}"


def test_stop_survives_plex_credentials_going_away(plex):
    creds: list = [(PLEX_URL, TOKEN)]
    manager = StreamManager(lambda: creds[0], session=plex)
    stream_id = _start(manager)
    creds[0] = None

    manager.stop(stream_id, "alice")


def test_the_default_session_has_a_wide_connection_pool():
    manager = StreamManager(lambda: None)

    adapter = manager._session.get_adapter("https://plex.test/")

    assert adapter._pool_maxsize == 32


def test_a_failure_that_is_not_a_plex_error_still_gives_the_session_back(manager, plex):
    def explode(session):
        raise RuntimeError("boom")

    plex.on_start = explode

    with pytest.raises(RuntimeError):
        _start(manager)

    session = plex.calls_to("decision")[0]["params"]["session"]
    assert [c["params"]["session"] for c in plex.calls_to("stop")] == [session]
    assert plex.calls_to("decision")[1:] == []


def test_a_failing_cleanup_does_not_hide_the_original_error(manager, plex, monkeypatch):
    def explode(session):
        raise RuntimeError("boom")

    plex.on_start = explode
    monkeypatch.setattr(plex_stream, "resolve_safe_outbound_url", _refuse_after_the_first_calls())

    with pytest.raises(RuntimeError, match="boom"):
        _start(manager)


def _refuse_after_the_first_calls():
    calls = []

    def resolve(url, **kwargs):
        calls.append(url)
        if url.endswith("/stop"):
            raise ValueError("bad host")
        return True, "plex.test", ("203.0.113.5",)

    return resolve


def test_a_start_that_runs_past_its_deadline_fails_before_the_next_plex_call(manager, plex, clock):
    plex.on_call = lambda path: setattr(clock, "now", clock.now + 25)

    with pytest.raises(PlexUnavailable):
        _start(manager)

    # decision at 0 s, start at 25 s, then the child playlist would begin at 50 s, past 45 s
    assert len(plex.calls_to("decision")) == 1
    assert len(plex.calls_to("stop")) == 1
    assert not any(p.endswith("index.m3u8") for p in plex.paths())


def test_a_slow_plex_is_not_retried_once_the_deadline_has_passed(manager, plex, clock):
    plex.fail_decisions = 2
    plex.on_call = lambda path: setattr(clock, "now", clock.now + 50)

    with pytest.raises(PlexUnavailable):
        _start(manager)

    assert len(plex.calls_to("decision")) == 1


def test_pings_use_a_short_timeout(manager, plex, clock):
    _start(manager)

    manager.reap(clock.now)

    assert plex.calls_to("ping")[0]["timeout"][1] <= 5


def test_a_reaper_round_stops_pinging_when_plex_is_slow(manager, plex, clock):
    for key in ("1", "2", "3"):
        _start(manager, rating_key=key)
    plex.on_call = lambda path: (
        setattr(clock, "now", clock.now + 40) if path.endswith("/ping") else None
    )

    manager.reap(clock.now)

    assert len(plex.calls_to("ping")) == 2


def test_stop_all_stops_every_live_stream_on_plex(manager, plex):
    _start(manager, rating_key="1")
    _start(manager, user="bob", rating_key="2")

    manager.stop_all()

    assert len(plex.calls_to("stop")) == 2
    assert manager.reap() == 0
    assert plex.calls_to("ping") == []


def test_stop_all_gives_up_waiting_on_a_hung_plex(manager, plex):
    release = threading.Event()
    _start(manager)
    plex.on_call = lambda path: release.wait(10) if path.endswith("/stop") else None

    began = time.monotonic()
    manager.stop_all(budget=0.2)
    waited = time.monotonic() - began
    release.set()

    assert waited < 3


def test_a_two_hour_pause_still_credits_the_whole_viewing(manager, clock):
    first = _start(manager)
    clock.now += 240
    manager.get(first, "alice")
    clock.now += 7200  # paused for two hours; the player then starts a new stream
    second = _start(manager)
    clock.now += 120

    assert manager.get(second, "alice").elapsed_s >= 5 * 60


def _decision(video: str = "copy", extra: str = "") -> bytes:
    return (
        '<MediaContainer><Video><Media selected="1"><Part decision="transcode">'
        f'<Stream streamType="1" decision="{video}"/><Stream streamType="2" decision="transcode"/>'
        f"{extra}</Part></Media></Video></MediaContainer>"
    ).encode()


def test_an_original_plex_copies_is_asked_for_without_a_size_or_bitrate(manager, plex):
    plex.decision = _decision()

    stream_id = _start(manager, quality="original", fallback="720p-4000")

    assert manager.get(stream_id, "alice").quality == "original"
    for call in plex.calls_to("decision") + plex.calls_to("start.m3u8"):
        assert call["params"]["directStream"] == "1"
        assert not {"videoResolution", "maxVideoBitrate"} & call["params"].keys()


def _not_copied(plex):
    plex.decision = _decision("transcode")


def _unservable(plex):
    plex.decision = _decision()
    playlist = CHILD_PLAYLIST.replace("#EXTINF:8", '#EXT-X-MAP:URI="header"\n#EXTINF:8')
    plex.child_playlist = playlist
    plex.on_call = lambda path: (
        setattr(plex, "child_playlist", CHILD_PLAYLIST) if path.endswith("/stop") else None
    )


def _start_fails(plex):
    plex.decision = _decision()
    plex.fail_decisions = 1


@pytest.mark.parametrize("failure", [_not_copied, _unservable, _start_fails])
def test_an_original_that_fails_starts_the_fallback_and_gives_its_session_back(
    manager, plex, failure
):
    failure(plex)

    stream_id = _start(manager, quality="original", fallback="720p-4000")

    assert manager.get(stream_id, "alice").quality == "720p-4000"
    last = plex.calls_to("start.m3u8")[-1]["params"]
    assert (last["videoResolution"], last["maxVideoBitrate"]) == ("1280x720", "4000")
    first = plex.calls_to("decision")[0]["params"]["session"]
    assert [c["params"]["session"] for c in plex.calls_to("stop")] == [first]


OTHER_VERSION = b'<Media><Part><Stream streamType="1" codec="hevc"/></Part></Media>'


@pytest.mark.parametrize(
    ("body", "copied"),
    [
        (_decision(), True),
        (_decision("transcode"), False),
        (_decision(extra='<Stream streamType="3" decision="burn"/>'), False),
        (_decision().replace(b"<Video>", b"<Video>" + OTHER_VERSION), True),
        (b"<MediaContainer/>", False),
        (b"not xml", False),
        (b'<!DOCTYPE a [<!ENTITY b "c">]><MediaContainer>&b;</MediaContainer>', False),
    ],
    ids=["copy", "transcode", "burned", "other version", "no media", "unreadable", "entities"],
)
def test_the_decision_says_whether_the_video_is_copied(manager, plex, body, copied):
    plex.decision = body

    assert manager.copies_video("123", "file") is copied

    assert plex.paths() == [f"{TRANSCODE}/decision", f"{TRANSCODE}/stop"]
    decision, stop = plex.calls
    assert stop["params"]["session"] == decision["params"]["session"]
    assert {c["method"] for c in plex.calls} == {"GET"}
    assert stop["timeout"] == plex_stream._QUIET_TIMEOUT


def test_the_copy_verdict_is_asked_once_per_file_until_it_is_old(manager, plex, clock):
    plex.decision = _decision()
    for _ in range(3):
        assert manager.copies_video("123", "file") is True
    assert len(plex.calls_to("decision")) == 1

    plex.decision = _decision("transcode")
    assert manager.copies_video("123", "replaced file") is False
    clock.now += 3601
    assert manager.copies_video("123", "file") is False


def test_a_failed_question_is_no_copy_and_is_asked_again_after_a_minute(manager, plex, clock):
    plex.decision = _decision()
    plex.fail_decisions = 1

    assert manager.copies_video("123", "file") is False
    clock.now += 59
    assert manager.copies_video("123", "file") is False
    assert len(plex.calls_to("decision")) == 1
    clock.now += 2
    assert manager.copies_video("123", "file") is True


def test_a_failed_question_keeps_the_answer_plex_gave_before(manager, plex, clock):
    plex.decision = _decision()
    assert manager.copies_video("123", "file") is True
    clock.now += 3601
    plex.fail_decisions = 1

    assert manager.copies_video("123", "file") is True
    assert manager.copies_video("123", "file") is True
    assert len(plex.calls_to("decision")) == 2


def test_a_restart_reuses_the_answer_the_viewing_began_with(manager, plex, clock):
    plex.decision = _decision()
    assert manager.copies_video("123", "file", "alice") is True
    stream_id = _start(manager)
    clock.now += 3601
    manager.playlist(stream_id, "alice")
    manager.stop(stream_id, "alice")

    assert manager.copies_video("123", "file", "alice") is True
    assert len(plex.calls_to("decision")) == 2  # the first question and the start's own
    assert manager.copies_video("123", "file", "bob") is True
    assert len(plex.calls_to("decision")) == 3


def _unreadable_start(plex):
    plex.decision = _decision()
    plex.variant = "http://elsewhere/index.m3u8"
    plex.on_call = lambda path: setattr(plex, "variant", None) if path.endswith("/stop") else None


def _unreachable(plex):
    plex.decision = _decision()
    plex.unreachable = True
    plex.on_call = lambda path: (
        setattr(plex, "unreachable", False) if path.endswith("/stop") else None
    )


@pytest.mark.parametrize(
    "failure", [_not_copied, _unservable, _unreadable_start, _start_fails, _unreachable]
)
def test_an_original_that_failed_to_start_is_not_offered_again(manager, plex, failure):
    plex.decision = _decision()
    assert manager.copies_video("123", "file") is True
    assert manager.copies_video("456", "file") is True
    failure(plex)
    _start(manager, quality="original", fallback="720p-4000")

    assert manager.copies_video("123", "file") is False
    assert manager.copies_video("456", "file") is True


@pytest.mark.parametrize("failure", [_not_copied, _unservable, _unreadable_start])
def test_an_original_that_cannot_be_played_is_held_off_for_an_hour(manager, plex, clock, failure):
    plex.decision = _decision()
    manager.copies_video("123", "file")
    failure(plex)
    _start(manager, quality="original", fallback="720p-4000")
    plex.decision = _decision()

    clock.now += 3599
    assert manager.copies_video("123", "file") is False
    clock.now += 2
    assert manager.copies_video("123", "file") is True


@pytest.mark.parametrize("failure", [_start_fails, _unreachable])
def test_an_original_plex_only_failed_to_start_is_asked_about_again_after_a_minute(
    manager, plex, clock, failure
):
    plex.decision = _decision()
    manager.copies_video("123", "file")
    failure(plex)
    _start(manager, "alice", quality="original", fallback="720p-4000")

    clock.now += 59
    assert manager.copies_video("123", "file", "alice") is False
    clock.now += 2
    # Even for the viewing that met the failure.
    assert manager.copies_video("123", "file", "alice") is True


def test_an_original_is_still_offered_after_plex_could_start_nothing(manager, plex):
    plex.decision = _decision()
    assert manager.copies_video("123", "file") is True
    plex.fail_decisions = 2
    with pytest.raises(PlexUnavailable):
        _start(manager, quality="original", fallback="720p-4000")

    assert manager.copies_video("123", "file") is True


def test_peeking_at_a_stream_does_not_keep_it_from_being_reaped(manager, clock):
    stream_id = _start(manager)
    clock.now += 170
    assert manager.peek(stream_id, "alice").rating_key == "123"
    clock.now += 170

    assert manager.reap(clock.now) == 1
    with pytest.raises(StreamGone):
        manager.peek(stream_id, "alice")


def test_someone_elses_stream_cannot_be_peeked_at(manager):
    stream_id = _start(manager, user="alice")

    with pytest.raises(StreamGone):
        manager.peek(stream_id, "bob")


def _fill(manager: StreamManager, stream_id: str, user: str) -> list:
    reads = []
    with contextlib.suppress(TooManyReads):
        while len(reads) < 100:
            read = SegmentRead(stream_id, user)
            manager.claim_read(read)
            reads.append(read)
    return reads


def test_a_stream_reads_two_segments_at_once_and_goes_on_once_one_is_given_back(manager):
    stream_id = _start(manager)
    reads = _fill(manager, stream_id, "alice")
    assert len(reads) == MAX_READS_PER_STREAM == 2

    manager.release_read(reads[0])
    manager.release_read(reads[0])

    assert len(_fill(manager, stream_id, "alice")) == 1


def test_a_full_stream_leaves_the_same_persons_other_titles_and_other_people_alone(manager):
    for key in range(MAX_STREAMS_PER_USER):
        assert len(_fill(manager, _start(manager, rating_key=str(key)), "alice")) == 2
    assert len(_fill(manager, _start(manager, user="bob"), "bob")) == 2


def test_starting_anew_does_not_lift_the_limit_while_the_old_reads_are_held(manager):
    held = 0
    for _ in range(MAX_READS_PER_USER):
        held += len(_fill(manager, _start(manager), "alice"))
    assert held == MAX_READS_PER_USER

    assert len(_fill(manager, _start(manager, user="bob"), "bob")) == 2


def test_reads_in_all_stay_well_below_the_threads_the_pages_share(manager):
    held = sum(
        len(_fill(manager, _start(manager, user=f"user{n}"), f"user{n}"))
        for n in range(MAX_STREAMS_TOTAL)
    )
    assert held == MAX_READS_TOTAL <= anyio.run(_default_threads) // 2


async def _default_threads() -> float:
    return anyio.to_thread.current_default_thread_limiter().total_tokens


def _body_of(plex, chunks):
    """Make the fake Plex answer every later call with *chunks*, read one by one."""
    response = FakeResponse(200)
    response.iter_content = lambda chunk_size: chunks
    plex.request = lambda *args, **kwargs: response


@pytest.mark.parametrize("why", ["stopped", "unwanted"])
def test_a_read_ends_midway_once_its_stream_is_stopped_or_nobody_wants_it(manager, plex, why):
    stream_id = _start(manager)
    read = SegmentRead(stream_id, "alice")

    def chunks():
        yield b"one"
        if why == "stopped":
            manager.stop(stream_id, "alice")
        else:
            read.cancel()
        yield b"two"
        pytest.fail("read on")

    _body_of(plex, chunks())

    with pytest.raises(StreamGone):
        manager.segment(read, "00001.ts")


def test_an_unwanted_read_never_asks_plex(manager, plex):
    stream_id = _start(manager)
    read = SegmentRead(stream_id, "alice")
    read.cancel()
    asked = len(plex.calls)

    with pytest.raises(StreamGone):
        manager.segment(read, "00001.ts")
    assert len(plex.calls) == asked


def test_a_segment_that_trickles_in_is_given_up(manager, plex, clock):
    stream_id = _start(manager)
    read = SegmentRead(stream_id, "alice")

    def chunks():
        for _ in range(10):
            clock.now += 20
            yield b"drop"
        pytest.fail("read on")

    _body_of(plex, chunks())

    with pytest.raises(PlexUnavailable):
        manager.segment(read, "00001.ts")


def test_a_start_that_fails_leaves_the_one_before_gone_and_not_replaced(manager, plex):
    stream_id = _start(manager)
    plex.unreachable = True
    with pytest.raises(PlexUnavailable):
        _start(manager)

    with pytest.raises(StreamGone) as caught:
        manager.get(stream_id, "alice")
    assert not isinstance(caught.value, StreamReplaced)


def test_a_failed_start_never_hides_that_another_start_replaced_the_stream(manager, plex):
    stream_id = _start(manager)
    real_open = manager._open_session
    opening = []

    def another_starts_then_this_fails(*args):
        if opening:
            return real_open(*args)
        opening.append(True)
        _start(manager)
        raise RuntimeError("failed")

    manager._open_session = another_starts_then_this_fails
    with pytest.raises(RuntimeError):
        _start(manager)

    with pytest.raises(StreamReplaced):
        manager.get(stream_id, "alice")


@pytest.mark.parametrize("succeeds", [True, False])
def test_a_failed_start_leaves_it_to_a_later_one_still_opening_whether_the_stream_is_replaced(
    manager, succeeds
):
    stream_id = _start(manager)
    real_open = manager._open_session
    begun, failed = [], threading.Event()
    outcomes = []

    def later_opens_meanwhile(*args):
        begun.append(True)
        if len(begun) == 1:
            later.start()
            _wait_until(lambda: len(begun) == 2)
            raise RuntimeError("failed")
        assert failed.wait(5)
        with pytest.raises(StreamReplaced):
            manager.get(stream_id, "alice")
        if not succeeds:
            raise RuntimeError("failed")
        return real_open(*args)

    def start_later():
        with contextlib.suppress(RuntimeError):
            _start(manager)
        with pytest.raises(StreamGone) as caught:
            manager.get(stream_id, "alice")
        outcomes.append(isinstance(caught.value, StreamReplaced))

    later = threading.Thread(target=start_later, daemon=True)
    manager._open_session = later_opens_meanwhile
    with pytest.raises(RuntimeError):
        _start(manager)
    failed.set()
    later.join(5)

    assert outcomes == [succeeds]


def _wait_until(condition) -> None:
    for _ in range(1000):
        if condition():
            return
        time.sleep(0.005)
    pytest.fail("never came true")


def test_a_start_held_open_past_the_beginning_of_a_later_one_makes_way_for_it(manager, plex):
    real_open = manager._open_session
    outer, inner = [], []
    second_opens, first_stored = threading.Event(), threading.Event()

    def open_in_turn(*args):
        if not outer:
            outer.append(True)
            later.start()
            assert second_opens.wait(5)
        else:
            second_opens.set()
            assert first_stored.wait(5)
        return real_open(*args)

    later = threading.Thread(target=lambda: inner.append(_start(manager)), daemon=True)
    manager._open_session = open_in_turn

    first = _start(manager)
    assert manager.get(first, "alice")
    first_stored.set()
    later.join(5)

    assert manager.get(inner[0], "alice")
    with pytest.raises(StreamReplaced):
        manager.get(first, "alice")
    sessions = [c["params"]["session"] for c in plex.calls_to("start.m3u8")]
    assert [c["params"]["session"] for c in plex.calls_to("stop")] == [sessions[0]]
    assert len(manager._streams) == 1


def test_a_stopped_stream_is_gone_and_not_replaced(manager):
    stopped = _start(manager)
    manager.stop(stopped, "alice")
    _start(manager)

    assert not _is_replaced(manager, stopped)


def _is_replaced(manager, stream_id, user="alice") -> bool:
    with pytest.raises(StreamGone) as caught:
        manager.get(stream_id, user)
    return isinstance(caught.value, StreamReplaced)


def _reap_one_idle(manager, clock) -> None:
    clock.now += plex_stream.IDLE_TIMEOUT_SECONDS + 1
    assert manager.reap() == 1


def test_a_reaped_stream_is_replaced_once_the_item_was_started_anew(manager, clock):
    idle = _start(manager)
    _reap_one_idle(manager, clock)
    clock.now += plex_stream.REAPED_KEEP_SECONDS + 1
    manager.reap()
    later = _start(manager)

    assert _is_replaced(manager, idle)
    manager.stop(later, "alice")
    assert _is_replaced(manager, idle)
    assert not _is_replaced(manager, idle, "bob")


def test_a_reaped_stream_is_only_gone_where_its_person_has_not_started_the_item_since(
    manager, clock
):
    _start(manager)
    idle = _start(manager)
    _reap_one_idle(manager, clock)
    _start(manager, rating_key="7")
    _start(manager, user="bob")

    assert not _is_replaced(manager, idle)


def test_a_reaped_stream_is_only_gone_while_a_start_of_the_item_opens_and_after_it_failed(
    manager, plex, clock
):
    idle = _start(manager)
    _reap_one_idle(manager, clock)
    seen = []

    def look_then_fail(*args):
        seen.append(_is_replaced(manager, idle))
        raise RuntimeError("failed")

    manager._open_session = look_then_fail
    with pytest.raises(RuntimeError):
        _start(manager)

    assert seen == [False]
    assert not _is_replaced(manager, idle)


def test_a_reaped_stream_is_forgotten_once_its_viewing_is_over(manager, clock):
    idle = _start(manager)
    _reap_one_idle(manager, clock)
    clock.now += plex_stream.VIEWING_KEEP_SECONDS + 1
    manager.reap()
    _start(manager)

    assert not _is_replaced(manager, idle)
    assert not manager._reaped


def _replaced_by(manager, stream_id, user="alice") -> str | None:
    with pytest.raises(StreamReplaced) as caught:
        manager.get(stream_id, user)
    return caught.value.by


def test_a_start_a_page_makes_by_itself_leaves_the_title_to_the_page_asked_later(
    manager, plex, clock
):
    phone = _start(manager, page="phone-page")
    clock.now += 10
    tv = _start(manager, page="tv-page")
    clock.now += 5
    stops = len(plex.calls_to("stop"))

    with pytest.raises(StreamReplaced) as refused:
        _start(manager, page="phone-page", asked_ago=15.0)

    assert (refused.value.by, _replaced_by(manager, phone)) == ("tv-page", "tv-page")
    assert manager.get(tv, "alice")
    assert len(plex.calls_to("stop")) == stops
    assert len(plex.calls_to("start.m3u8")) == 2


def test_a_press_always_takes_the_title_and_so_does_a_try_of_a_press_made_later(manager, clock):
    tv = _start(manager, page="tv-page")
    clock.now += 10
    # Pressed five seconds ago on the phone, after the television: only now does it get through.
    phone = _start(manager, page="phone-page", asked_ago=5.0)
    assert (_replaced_by(manager, tv), bool(manager.get(phone, "alice"))) == ("phone-page", True)

    again = _start(manager, page="tv-page")
    assert (_replaced_by(manager, phone), bool(manager.get(again, "alice"))) == ("tv-page", True)


def test_a_page_s_own_later_start_and_a_start_with_no_page_replace_as_before(manager, clock):
    first = _start(manager, page="phone-page")
    clock.now += 60
    second = _start(manager, page="phone-page", asked_ago=60.0)
    assert _replaced_by(manager, first) == "phone-page"

    clock.now += 60
    third = _start(manager)
    assert _replaced_by(manager, second) is None
    # What has no page was asked for as it came: later than the phone's own press.
    with pytest.raises(StreamReplaced):
        _start(manager, page="phone-page", asked_ago=120.0)
    assert manager.get(third, "alice")


def test_a_start_made_by_itself_makes_way_for_a_later_press_that_is_still_opening(manager, clock):
    _start(manager, page="phone-page")
    clock.now += 30
    real_open = manager._open_session
    refused = []

    def phone_tries_meanwhile(*args):
        manager._open_session = real_open
        with pytest.raises(StreamReplaced) as caught:
            _start(manager, page="phone-page", asked_ago=30.0)
        refused.append(caught.value.by)
        return real_open(*args)

    manager._open_session = phone_tries_meanwhile
    tv = _start(manager, page="tv-page")

    assert refused == ["tv-page"]
    assert manager.get(tv, "alice")


def test_a_reaped_stream_names_the_page_that_started_the_item_since(manager, clock):
    idle = _start(manager, page="phone-page")
    _reap_one_idle(manager, clock)
    _start(manager, page="tv-page")

    assert _replaced_by(manager, idle) == "tv-page"


class SlowPlex:
    """A real server that gives each request the next of *answers*, and keeps its connections."""

    def __init__(self, answers: list[bytes], cert: Path | None = None) -> None:
        self._answers = iter(answers)
        self._tls = None
        if cert is not None:
            self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._tls.load_cert_chain(cert)
        self._listener = socket.create_server(("127.0.0.1", 0))
        scheme = "https" if cert else "http"
        self.url = f"{scheme}://plex.test:{self._listener.getsockname()[1]}"
        self.latest: socket.socket
        self.asked = threading.Semaphore(0)
        self.hung_up = threading.Semaphore(0)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        with contextlib.suppress(OSError):
            while True:
                conn = self._listener.accept()[0]
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.settimeout(10)
            if self._tls:
                conn = self._tls.wrap_socket(conn, server_side=True)
            with conn:
                # A hang-up with bytes still on their way to the client arrives as a reset.
                with contextlib.suppress(ConnectionError):
                    while self._read_request(conn):
                        conn.sendall(next(self._answers))
                        self.latest = conn
                        self.asked.release()
                self.hung_up.release()

    def _read_request(self, conn: socket.socket) -> bool:
        request = b""
        while b"\r\n\r\n" not in request:
            more = conn.recv(65536)
            if not more:
                return False
            request += more
        return True

    def close(self) -> None:
        self._listener.close()


def _answer(body: bytes, length: int | None = None) -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (
        len(body) if length is None else length,
        body,
    )


@pytest.fixture(scope="module")
def cert(tmp_path_factory) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "plex.test")])
    made = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2000, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=UTC))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("plex.test")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path_factory.mktemp("plex") / "plex.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + made.public_bytes(serialization.Encoding.PEM)
    )
    return path


@pytest.fixture
def slow_plex(plex, monkeypatch, request):
    """Start a SlowPlex: ``(server, manager, stream id)``, the stream's segments read from it."""
    monkeypatch.setattr(
        plex_stream,
        "resolve_safe_outbound_url",
        lambda url, **kwargs: (True, "plex.test", ("127.0.0.1",)),
    )

    def start(answers: list[bytes], cert: Path | None = None):
        server = SlowPlex(answers, cert)
        request.addfinalizer(server.close)
        manager = StreamManager(lambda: (server.url, TOKEN), session=plex)
        stream_id = _start(manager)
        manager._session = StreamManager(lambda: None)._session
        manager._session.verify = str(cert) if cert else True
        request.addfinalizer(manager._session.close)
        return server, manager, stream_id

    return start


def _in_thread(call):
    """Run *call* in a thread; the function returned waits for it and gives back its outcome."""
    outcome = []

    def run():
        try:
            outcome.append(call())
        except Exception as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def wait():
        thread.join(2)
        assert outcome, "still waiting"
        return outcome[0]

    return wait


@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
@pytest.mark.parametrize(
    "sent", [b"", _answer(b"some", length=999999)], ids=["headers", "body of a kept connection"]
)
def test_cancelling_a_read_hangs_up_on_plex_whatever_it_waits_for(
    slow_plex, cert, caplog, sent, tls
):
    server, manager, stream_id = slow_plex([sent], cert if tls else None)
    read = SegmentRead(stream_id, "alice")
    ended = _in_thread(lambda: manager.segment(read, "00001.ts"))
    assert server.asked.acquire(timeout=5)
    if sent:
        # Past the headers, into the wait for the rest of the body.
        time.sleep(0.05)

    read.cancel()

    outcome = ended()
    assert isinstance(outcome, StreamGone)
    assert outcome.__context__ is None
    assert server.hung_up.acquire(timeout=2)
    assert "could not reach Plex" not in caplog.text


def test_a_late_cancel_leaves_the_next_request_of_the_same_thread_alone(slow_plex):
    _, manager, stream_id = slow_plex([_answer(b"video"), _answer(CHILD_PLAYLIST.encode())])
    read = SegmentRead(stream_id, "alice")
    assert manager.segment(read, "00001.ts")[0] == b"video"

    read.cancel()

    assert "#EXTINF" in manager.playlist(stream_id, "alice")


def test_a_late_cancel_leaves_another_readers_connection_alone(slow_plex):
    server, manager, stream_id = slow_plex([_answer(b"video"), _answer(b"some", length=8)])
    early = SegmentRead(stream_id, "alice")
    assert manager.segment(early, "00001.ts")[0] == b"video"
    assert server.asked.acquire(timeout=5)
    later = _in_thread(lambda: manager.segment(SegmentRead(stream_id, "alice"), "00002.ts"))
    assert server.asked.acquire(timeout=5)

    early.cancel()

    with contextlib.suppress(OSError):
        server.latest.sendall(b"rest")
    assert later()[0] == b"somerest"


@pytest.mark.parametrize(
    "answer",
    [
        b"HTTP/1.1 500 No\r\nContent-Length: 9\r\n\r\nsome",
        _answer(b"some", length=plex_stream._SEGMENT_MAX_BYTES + 1),
    ],
    ids=["not 200", "over the cap"],
)
def test_an_answer_given_up_on_is_hung_up(slow_plex, monkeypatch, answer):
    server, manager, stream_id = slow_plex([answer])
    real = manager._session.request
    # Held here, or its collection would hang up.
    responses = []

    def request(*args, **kwargs):
        responses.append(real(*args, **kwargs))
        return responses[-1]

    monkeypatch.setattr(manager._session, "request", request)
    with pytest.raises(PlexUnavailable):
        manager.segment(SegmentRead(stream_id, "alice"), "00001.ts")

    assert server.hung_up.acquire(timeout=2)


def test_a_person_s_starts_at_once_are_bounded_and_one_that_ends_makes_room(manager):
    with contextlib.ExitStack() as held:
        for _ in range(MAX_STARTS_PER_USER - 1):
            held.enter_context(manager.starting("alice"))
        with manager.starting("alice"), pytest.raises(TooManyStarts), manager.starting("alice"):
            pass
        with manager.starting("alice"), manager.starting("bob"):
            pass


def test_starts_at_once_leave_threads_to_the_pages(manager):
    with contextlib.ExitStack() as held:
        for n in range(MAX_STARTS_TOTAL):
            held.enter_context(manager.starting(f"user{n}"))
        with pytest.raises(TooManyStarts), manager.starting("one-more"):
            pass
    assert anyio.run(_default_threads) * 2 / 3 >= MAX_STARTS_TOTAL + MAX_READS_TOTAL


@pytest.mark.parametrize("hangs", [False, True])
def test_a_press_past_the_limit_ends_the_person_s_oldest_start_only_if_it_hangs(
    slow_plex, clock, hangs
):
    server, manager, _ = slow_plex([b"", b"HTTP/1.1 500 No\r\nContent-Length: 0\r\n\r\n"])
    manager._clock = clock

    def hung():
        with manager.starting("alice"):
            return _start(manager, rating_key="7")

    ended = _in_thread(hung)
    assert server.asked.acquire(timeout=5)
    manager._call_quietly = lambda *args: None
    clock.now += plex_stream.HUNG_START_SECONDS if hangs else 1
    with contextlib.ExitStack() as held:
        for _ in range(MAX_STARTS_PER_USER - 1):
            held.enter_context(manager.starting("alice"))
        with pytest.raises(TooManyStarts), manager.starting("alice"):
            pass
        if hangs:
            assert isinstance(ended(), TooManyStarts)
            with manager.starting("alice"):
                pass
        else:
            server.latest.shutdown(socket.SHUT_RDWR)
            assert isinstance(ended(), PlexUnavailable)


def test_giving_a_session_back_is_not_hung_up_with_its_start(slow_plex):
    server, manager, _ = slow_plex([b"", _answer(b"")])

    def hung():
        with manager.starting("alice"):
            return _start(manager, rating_key="7")

    ended = _in_thread(hung)
    assert server.asked.acquire(timeout=5)
    [start] = manager._starting

    start.cancel()

    assert isinstance(ended(), TooManyStarts)
    assert server.asked.acquire(timeout=2)


def test_a_start_made_by_itself_goes_through_once_the_page_asked_later_stopped_its_stream(
    manager, clock
):
    clock.now += 10
    phone = _start(manager, page="phone-page")
    manager.stop(phone, "alice")
    clock.now += 5

    assert manager.get(_start(manager, page="tv-page", asked_ago=15.0), "alice")


def test_stopping_a_stream_leaves_the_title_to_a_later_press_still_opening(manager, clock):
    real_open = manager._open_session
    phone = _start(manager, page="phone-page")
    clock.now += 1
    refused = []

    def tv_tries_meanwhile(*args):
        manager._open_session = real_open
        with contextlib.suppress(StreamGone):
            manager.stop(phone, "alice")
        with pytest.raises(StreamReplaced) as caught:
            _start(manager, page="tv-page", asked_ago=5.0)
        refused.append(caught.value.by)
        return real_open(*args)

    manager._open_session = tv_tries_meanwhile
    _start(manager, page="den-page")

    assert refused == ["den-page"]


def test_a_start_made_by_itself_is_refused_where_the_later_page_s_stream_was_reaped(manager, clock):
    phone = _start(manager, page="phone-page")
    _reap_one_idle(manager, clock)
    with pytest.raises(StreamGone):
        manager.stop(phone, "alice")

    with pytest.raises(StreamReplaced):
        _start(manager, page="tv-page", asked_ago=500.0)
