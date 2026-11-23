"""Tests for the Plex stream proxy service, against a fake Plex HTTP layer."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import pytest
import requests

from mediaman.services.media_meta import plex_stream
from mediaman.services.media_meta.plex_stream import (
    MAX_STREAMS_PER_USER,
    MAX_STREAMS_TOTAL,
    StreamGone,
    StreamManager,
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
        self.child_playlist = CHILD_PLAYLIST
        self.sessions: list[dict] = []
        self.unreachable = False
        self.prefix = ""
        self.on_start = None

    def request(self, method, url, *, params=None, headers=None, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params or {}), "headers": dict(headers)}
        )
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
            return FakeResponse(200, b"<MediaContainer/>")
        if path == f"{TRANSCODE}/start.m3u8":
            if self.on_start:
                self.on_start(session)
            if session not in self.decided:
                return FakeResponse(400)
            master = (
                f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=8000000\nsession/{session}/base/index.m3u8\n"
            )
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

    body, content_type = manager.segment(stream_id, "alice", "00001.ts")

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
        manager.segment(stream_id, "alice", name)

    assert len(plex.calls) == calls_before


def test_another_users_stream_is_indistinguishable_from_an_unknown_one(manager):
    stream_id = _start(manager, user="alice")

    with pytest.raises(StreamGone) as unknown:
        manager.playlist("no-such-stream", "bob")
    for attempt in (
        lambda: manager.playlist(stream_id, "bob"),
        lambda: manager.segment(stream_id, "bob", "00000.ts"),
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
    manager.segment(stream_id, "alice", "00000.ts")
    clock.now += 170

    assert manager.reap(clock.now) == 0


def test_a_failed_start_is_retried_once_with_a_fresh_session(manager, plex):
    plex.fail_decisions = 1

    stream_id = _start(manager)

    sessions = [c["params"]["session"] for c in plex.calls_to("decision")]
    assert len(sessions) == 2
    assert sessions[0] != sessions[1]
    assert [c["params"]["session"] for c in plex.calls_to("stop")] == [sessions[0]]
    manager.segment(stream_id, "alice", "00000.ts")


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
    manager.segment(stream_id, "alice", "00001.ts")
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


def test_two_starts_of_the_same_item_racing_leave_one_stream_and_stop_the_other(manager, plex):
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
    assert manager.get(outer, "alice")
    with pytest.raises(StreamGone):
        manager.get(inner[0], "alice")
    sessions = [c["params"]["session"] for c in plex.calls_to("start.m3u8")]
    stopped = [c["params"]["session"] for c in plex.calls_to("stop")]
    assert stopped == [sessions[1]]


def test_plex_served_under_a_path_prefix_keeps_the_prefix(plex):
    plex.prefix = "/plex"
    manager = StreamManager(lambda: (f"{PLEX_URL}/plex/", TOKEN), session=plex)

    stream_id = _start(manager)
    manager.playlist(stream_id, "alice")
    manager.segment(stream_id, "alice", "00000.ts")
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
