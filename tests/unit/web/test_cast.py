"""Tests for the streams a TV reads with a ticket, against a fake Plex."""

from __future__ import annotations

import logging
import sqlite3
from urllib.parse import urlsplit

import defusedxml.ElementTree as ET
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from mediaman.config import Config
from mediaman.db import get_db
from mediaman.main import create_app
from mediaman.services.cast import tickets
from mediaman.services.cast.tickets import TICKET_SECONDS, TicketStore
from mediaman.services.media_meta import plex_stream
from mediaman.services.media_meta.plex_stream import (
    IDLE_TIMEOUT_SECONDS,
    TV_IDLE_TIMEOUT_SECONDS,
    StreamManager,
)
from mediaman.services.media_meta.plex_watch import PlexItemNotFound, PlexWatch
from mediaman.web.auth.password_hash import (
    change_password,
    create_user,
    delete_user,
    set_must_change_password,
)
from mediaman.web.auth.session_store import create_session, destroy_session
from mediaman.web.middleware import viewer_guard
from mediaman.web.routes import cast
from mediaman.web.routes.watch import stream_api

PLEX_URL = "http://plex-lan.example.test:32400"
TOKEN = "tok-Cq52-only-in-tests"
TRANSCODE = "/video/:/transcode/universal"
PLAYLIST = "#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXTINF:8.0,\n00000.ts\n#EXT-X-ENDLIST\n"

ITEM = """<MediaContainer librarySectionID="{section}"><Video type="movie" ratingKey="{key}"
 title="Film" duration="6000000"><Media><Part id="7">
 <Stream id="11" streamType="1"/>
 <Stream id="21" streamType="2" selected="1" displayTitle="English (AC3 5.1)"/>
 <Stream id="22" streamType="2" displayTitle="Polski (AAC Stereo)"/>
 <Stream id="31" streamType="3" displayTitle="English"/>
 <Stream id="32" streamType="3" displayTitle="Polski"/>
</Part></Media><Marker type="credits" final="1" startTimeOffset="5000000" endTimeOffset="6000000"/>
<Chapter tag="The boat" startTimeOffset="0" endTimeOffset="3000000"/>
<Chapter tag="The shore" startTimeOffset="3000000" endTimeOffset="6000000"/>
</Video></MediaContainer>"""


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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.knows_tracks = True
        self.subtitle_decision = "burn"
        self.reads_fail = False
        self.on_start = lambda: None

    def request(self, method, url, *, params=None, headers=None, **kwargs):
        path, params = urlsplit(url).path, dict(params or {})
        self.calls.append((path, params))
        if path == f"{TRANSCODE}/decision":
            audio = params.get("audioStreamID", "21") if self.knows_tracks else "21"
            sub = params.get("subtitleStreamID", "31") if self.knows_tracks else "31"
            return FakeResponse(
                200,
                f'<MediaContainer><Media><Stream id="{audio}" streamType="2" selected="1"/>'
                f'<Stream id="{sub}" streamType="3" decision="{self.subtitle_decision}"/></Media>'
                "</MediaContainer>".encode(),
            )
        if path == f"{TRANSCODE}/start.m3u8":
            self.on_start()
            return FakeResponse(
                200, f"#EXTM3U\nsession/{params['session']}/base/index.m3u8".encode()
            )
        if path.endswith("/base/index.m3u8"):
            return FakeResponse(500 if self.reads_fail else 200, PLAYLIST.encode())
        if path.endswith("/base/00000.ts"):
            return FakeResponse(500 if self.reads_fail else 200, b"video bytes")
        return FakeResponse(200, b"<MediaContainer/>")

    def decision(self) -> dict:
        return next(p for path, p in reversed(self.calls) if path == f"{TRANSCODE}/decision")


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def plex(monkeypatch, clock) -> FakePlex:
    fake = FakePlex()
    monkeypatch.setattr(
        plex_stream,
        "resolve_safe_outbound_url",
        lambda url, **kwargs: (True, "plex-lan.example.test", ("203.0.113.5",)),
    )
    manager = StreamManager(lambda: (PLEX_URL, TOKEN), session=fake, clock=clock)
    monkeypatch.setattr(plex_stream, "_manager", manager)
    monkeypatch.setattr(tickets, "_store", TicketStore(clock=clock))

    def metadata(self, key, query=""):
        if not key.startswith(("1", "3")):
            raise PlexItemNotFound("Item not found in Plex")
        root = ET.fromstring(ITEM.format(key=key, section="1" if key[0] == "1" else "9"))
        return root, root[0]

    monkeypatch.setattr(PlexWatch, "_metadata", metadata)
    return fake


@pytest.fixture
def app(app_factory, conn, plex):
    for key, value in (
        ("plex_url", PLEX_URL),
        ("plex_token", TOKEN),
        ("plex_libraries", '["1"]'),
    ):
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at)"
            " VALUES (?, ?, 0, datetime('now'))",
            (key, value),
        )
    conn.commit()
    return app_factory(cast.router, stream_api.router, conn=conn)


@pytest.fixture
def alice(app, conn, authed_client):
    return authed_client(app, conn, username="alice")


@pytest.fixture
def tv(app):
    return TestClient(app)


def _start(client, rating_key: str = "101", **body):
    return client.post("/api/cast/stream", json={"rating_key": rating_key, **body})


def _cast(client, rating_key: str = "101", **body) -> dict:
    resp = _start(client, rating_key, **body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _segment_url(url: str) -> str:
    return url.replace("index.m3u8", "seg/00000.ts")


def _ticket(url: str) -> str:
    return url.split("/")[3]


def test_a_tv_plays_the_stream_with_no_session(alice, tv):
    url = _cast(alice)["url"]
    assert url.startswith("/api/cast/")
    playlist = tv.get(url)
    assert playlist.status_code == 200
    assert playlist.headers["content-type"] == "application/vnd.apple.mpegurl"
    segment_url = next(line for line in playlist.text.splitlines() if line.startswith("/"))
    assert segment_url == _segment_url(url)
    segment = tv.get(segment_url)
    assert (segment.status_code, segment.content) == (200, b"video bytes")
    for resp in (playlist, segment):
        assert resp.headers["cache-control"] == "no-store, no-transform"
        assert resp.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in resp.headers


def test_head_answers_for_a_held_ticket_without_fetching_from_plex(alice, tv, plex):
    url = _cast(alice)["url"]
    before = len(plex.calls)
    for path, kind in (
        (url, "application/vnd.apple.mpegurl"),
        (_segment_url(url), "video/mp2t"),
    ):
        resp = tv.head(path)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == kind
        assert "content-length" not in resp.headers
    assert len(plex.calls) == before


def test_preflight_is_the_same_for_a_held_and_a_made_up_ticket(alice, tv):
    url = _cast(alice)["url"]
    real, made_up = tv.options(url), tv.options("/api/cast/nope/index.m3u8")
    assert real.status_code == made_up.status_code == 204
    assert real.headers == made_up.headers
    assert "Range" in real.headers["access-control-allow-headers"]
    assert tv.options(_segment_url(url)).status_code == 204


def test_starting_needs_a_session_and_is_not_open_to_other_origins(alice, tv):
    assert _start(tv).status_code == 401
    assert "access-control-allow-origin" not in _start(alice).headers


def _sign_out(client, conn, clock, started):
    destroy_session(conn, client.cookies["session_token"])


def _new_password(client, conn, clock, started):
    assert change_password(
        conn, "alice", "password1234", "another-long-pass-77", enforce_policy=False
    )


def _delete_user(client, conn, clock, started):
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('root', 'x', '2026-01-01', 'admin')"
    )
    conn.commit()
    (user_id,) = conn.execute("SELECT id FROM admin_users WHERE username='alice'").fetchone()
    assert delete_user(conn, user_id, "root")


def _user_row_gone(client, conn, clock, started):
    conn.execute("DELETE FROM admin_users WHERE username = 'alice'")
    conn.commit()


def _must_change(client, conn, clock, started):
    set_must_change_password(conn, "alice", True)


def _session_over(client, conn, clock, started):
    conn.execute("UPDATE admin_sessions SET expires_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()


def _stop(client, conn, clock, started):
    client.post(f"/api/watch/stream/{started['stream_id']}/stop")


def _start_anew(client, conn, clock, started):
    _cast(client)


def _reaped(client, conn, clock, started):
    clock.now += TV_IDLE_TIMEOUT_SECONDS + 1
    plex_stream.get_stream_manager().reap()


def _ran_out(client, conn, clock, started):
    clock.now += TICKET_SECONDS


@pytest.mark.parametrize(
    "end",
    [
        _sign_out,
        _new_password,
        _delete_user,
        _user_row_gone,
        _must_change,
        _session_over,
        _stop,
        _start_anew,
        _reaped,
        _ran_out,
    ],
)
def test_a_ticket_ends_like_one_that_never_was(alice, tv, conn, clock, end):
    started = _cast(alice)
    url = started["url"]
    assert tv.get(url).status_code == 200
    end(alice, conn, clock, started)
    never = tv.get(f"/api/cast/{'x' * 43}/index.m3u8")
    assert never.status_code == 404
    for path in (url, _segment_url(url)):
        resp = tv.get(path)
        assert (resp.status_code, resp.content) == (404, never.content)
        assert resp.headers == never.headers
        assert tv.head(path).status_code == 404


def test_a_ticket_does_not_come_back_when_its_user_may_watch_again(alice, tv, conn):
    url = _cast(alice)["url"]
    set_must_change_password(conn, "alice", True)
    assert tv.get(url).status_code == 404
    set_must_change_password(conn, "alice", False)
    assert tv.get(url).status_code == 404


def test_one_persons_sign_out_leaves_another_session_of_theirs_casting(alice, tv, conn):
    url = _cast(alice)["url"]
    destroy_session(conn, create_session(conn, "alice"))
    assert tv.get(url).status_code == 200


@pytest.mark.parametrize("name", ["index.m3u8", "0.exe", "..ts", "00000.ts.ts"])
def test_a_ticket_reads_nothing_but_segments(alice, tv, name):
    url = _cast(alice)["url"]
    never = tv.get(f"/api/cast/{'x' * 43}/index.m3u8")
    for method in (tv.get, tv.head):
        resp = method(url.replace("index.m3u8", f"seg/{name}"))
        assert resp.status_code == 404
    assert tv.get(url.replace("index.m3u8", f"seg/{name}")).content == never.content


def test_a_ticket_is_no_session_and_its_stream_id_is_no_ticket(alice, tv):
    started = _cast(alice)
    ticket = _ticket(started["url"])
    assert tv.get(f"/api/cast/{started['stream_id']}/index.m3u8").status_code == 404
    assert tv.get(f"/api/watch/stream/{started['stream_id']}/index.m3u8").status_code == 401
    tv.cookies.set("session_token", ticket)
    assert _start(tv).status_code == 401


def test_bogus_tickets_never_stop_a_tv_at_the_same_address(alice, tv, monkeypatch):
    url = _cast(alice)["url"]
    monkeypatch.setattr(cast._FAILURE_LIMITER, "_max_attempts", 2)
    bogus = f"/api/cast/{'x' * 43}/index.m3u8"
    assert [tv.get(bogus).status_code for _ in range(3)] == [404, 404, 429]
    assert tv.get(url).status_code == 200
    assert tv.get(_segment_url(url)).status_code == 200


def test_past_the_limit_on_failures_every_bad_request_is_refused_alike(
    alice, tv, conn, monkeypatch
):
    url = _cast(alice)["url"]
    destroy_session(conn, alice.cookies["session_token"])
    monkeypatch.setattr(cast._FAILURE_LIMITER, "_max_attempts", 1)
    never = f"/api/cast/{'x' * 43}/index.m3u8"
    tv.get(never)
    ended, unknown = tv.get(url), tv.get(never)
    assert ended.status_code == unknown.status_code == 429
    assert (ended.content, ended.headers) == (unknown.content, unknown.headers)
    assert int(unknown.headers["retry-after"]) >= 1
    assert unknown.headers["access-control-allow-origin"] == "*"


def test_a_held_ticket_is_limited_by_its_own_stream_alone(alice, tv, monkeypatch):
    url, other = _cast(alice)["url"], _cast(alice, "102")["url"]
    monkeypatch.setattr(cast._STREAM_LIMITER, "_max_attempts", 2)
    assert [tv.get(url).status_code for _ in range(2)] == [200, 200]
    refused = tv.get(url)
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.headers["access-control-allow-origin"] == "*"
    assert tv.get(other).status_code == 200


def _fill(client) -> dict:
    started = [_cast(client, key) for key in ("101", "102", "103")]
    assert _start(client, "104").status_code == 429
    return started[0]


@pytest.mark.parametrize("end", [_must_change, _ran_out])
@pytest.mark.parametrize("notice", ["read", "reaper"])
def test_the_slot_comes_back_when_a_ticket_ends(alice, tv, conn, clock, end, notice):
    first = _fill(alice)
    tv.get(first["url"])
    end(alice, conn, clock, first)
    if notice == "read":
        assert tv.get(first["url"]).status_code == 404
    else:
        # Progress keeps the stream from being idle, so only its ticket's end can stop it.
        plex_stream.get_stream_manager().get(first["stream_id"], "alice")
        plex_stream.get_stream_manager().reap()
    set_must_change_password(conn, "alice", False)
    assert _start(alice, "104").status_code == 200


def test_a_place_saved_after_the_tv_was_refused_is_kept(alice, tv, conn, clock):
    started = _cast(alice)
    clock.now += TICKET_SECONDS
    assert tv.get(started["url"]).status_code == 404
    late = alice.post(
        "/api/watch/progress",
        json={"stream_id": started["stream_id"], "position_s": 1234, "played_s": 1234},
    )
    assert late.status_code == 410
    rows = conn.execute("SELECT username, position_s FROM watch_progress").fetchall()
    assert [tuple(r) for r in rows] == [("alice", 1234)]


@pytest.mark.parametrize("left", ["progress", "stop", "refused"])
@pytest.mark.parametrize(("position", "watched"), [(4990, 0), (5000, 1)])
def test_a_cast_left_in_the_final_credits_is_watched(
    alice, tv, conn, clock, left, position, watched
):
    started = _cast(alice)
    place = {"position_s": position, "played_s": 0}
    if left == "refused":
        clock.now += TICKET_SECONDS
        assert tv.get(started["url"]).status_code == 404
    if left == "stop":
        alice.post(f"/api/watch/stream/{started['stream_id']}/stop", json=place)
    else:
        alice.post("/api/watch/progress", json={"stream_id": started["stream_id"], **place})
    row = conn.execute("SELECT position_s, watched FROM watch_progress").fetchone()
    assert tuple(row) == (position, watched)


def test_the_session_ending_stops_the_stream_on_plex_without_a_read(alice, conn, plex):
    _cast(alice)
    session = plex.decision()["session"]
    destroy_session(conn, alice.cookies["session_token"])
    assert plex_stream.get_stream_manager().reap() == 1
    assert (f"{TRANSCODE}/stop", {"session": session}) in plex.calls


def test_a_session_that_cannot_be_checked_leaves_its_stream_and_the_others_reaped(
    alice, tv, clock, monkeypatch
):
    started = _cast(alice)
    tv.get(started["url"])
    idle = alice.post("/api/watch/stream", json={"rating_key": "102", "quality": "best"})
    clock.now += IDLE_TIMEOUT_SECONDS + 1

    def locked(conn, ticket):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patched:
        patched.setattr(cast, "_may_still_watch", locked)
        assert plex_stream.get_stream_manager().reap() == 1
    assert tv.get(started["url"]).status_code == 200
    assert alice.get(idle.json()["playlist"]).status_code == 410


@pytest.mark.parametrize("read", ["nothing", "head", "progress"])
def test_a_tv_that_never_reads_is_reaped_as_a_player_is(alice, tv, clock, read):
    started = _cast(alice)
    url = started["url"]
    if read == "head":
        tv.head(url)
    if read == "progress":
        plex_stream.get_stream_manager().get(started["stream_id"], "alice")
    clock.now += IDLE_TIMEOUT_SECONDS + 1
    assert plex_stream.get_stream_manager().reap() == 1
    assert tv.get(url).status_code == 404


@pytest.mark.parametrize("address", [str, _segment_url], ids=["playlist", "segment"])
def test_a_tvs_first_read_gives_it_the_longer_idle_time(alice, tv, clock, address):
    url = _cast(alice)["url"]
    assert tv.get(address(url)).status_code == 200
    clock.now += TV_IDLE_TIMEOUT_SECONDS - 1
    assert plex_stream.get_stream_manager().reap() == 0
    assert tv.get(url).status_code == 200


def test_a_sign_out_elsewhere_while_a_stream_starts_leaves_the_start(alice, tv, conn, plex):
    other = create_session(conn, "alice")
    plex.on_start = lambda: destroy_session(get_db(), other)
    assert tv.get(_cast(alice)["url"]).status_code == 200


def test_a_sign_out_elsewhere_while_a_player_stream_starts_leaves_the_start(alice, conn, plex):
    other = create_session(conn, "alice")
    plex.on_start = lambda: destroy_session(get_db(), other)
    started = alice.post("/api/watch/stream", json={"rating_key": "101", "quality": "best"})
    assert alice.get(started.json()["playlist"]).status_code == 200


def test_a_start_beaten_by_a_later_one_is_told_so(alice, plex):
    manager = plex_stream.get_stream_manager()
    later = iter([lambda: manager.start("alice", "101", "best", False, page="later-page")])
    plex.on_start = lambda: next(later, lambda: None)()
    resp = _start(alice)
    assert (resp.status_code, resp.json()["error"]) == (410, "replaced")
    assert "url" not in resp.json()


def test_the_stream_is_asked_of_plex_in_a_form_any_tv_plays(alice, plex):
    _cast(alice)
    params = plex.decision()
    assert (params["directPlay"], params["directStream"], params["directStreamAudio"]) == (
        "0",
        "0",
        "0",
    )
    assert params["videoResolution"] == "1920x1080"
    assert params["audioChannelCount"] == "2"
    profile = params["X-Plex-Client-Profile-Extra"]
    assert "videoCodec=h264&audioCodec=aac" in profile
    assert "video.level&value=41" in profile
    assert params["subtitles"] == "none"
    # The session started is the one decided on.
    started = next(p for path, p in plex.calls if path == f"{TRANSCODE}/start.m3u8")
    assert started == params


def test_the_players_own_streams_are_asked_for_as_before(alice, plex):
    alice.post("/api/watch/stream", json={"rating_key": "101", "quality": "best"})
    assert plex.decision()["directStream"] == "1"
    assert "X-Plex-Client-Profile-Extra" not in plex.decision()


def test_the_chosen_tracks_are_played_and_listed(alice, plex):
    started = _cast(alice, audio="22", subtitle="32", quality="medium")
    params = plex.decision()
    assert (params["audioStreamID"], params["subtitleStreamID"]) == ("22", "32")
    assert (params["subtitles"], params["videoResolution"]) == ("burn", "1280x720")
    assert (started["audio"], started["subtitle"]) == ("22", "32")
    assert started["audio_tracks"] == [
        {"id": "21", "label": "English (AC3 5.1)", "selected": True},
        {"id": "22", "label": "Polski (AAC Stereo)", "selected": False},
    ]
    assert [t["id"] for t in started["subtitle_tracks"]] == ["31", "32"]


def test_a_start_answers_the_marks_the_player_draws_and_skips_by(alice):
    started = _cast(alice)
    assert started["markers"] == [
        {"kind": "credits", "start_s": 5000.0, "end_s": 6000.0, "final": True}
    ]
    assert [(c["title"], c["start_s"]) for c in started["chapters"]] == [
        ("The boat", 0.0),
        ("The shore", 3000.0),
    ]


def test_without_a_choice_the_tracks_selected_in_plex_play(alice):
    started = _cast(alice)
    assert (started["audio"], started["subtitle"]) == ("21", None)


@pytest.mark.parametrize("choice", [{"audio": "31"}, {"subtitle": "999"}])
def test_a_track_the_title_does_not_have_is_refused_before_anything_starts(alice, plex, choice):
    resp = _start(alice, **choice)
    assert (resp.status_code, resp.json()["error"]) == (409, "track_unavailable")
    assert plex.calls == []


@pytest.mark.parametrize("choice", [{"audio": "22"}, {"subtitle": "32"}])
def test_a_track_plex_would_not_play_is_refused_and_its_session_given_back(alice, plex, choice):
    plex.knows_tracks = False
    resp = _start(alice, **choice)
    assert (resp.status_code, resp.json()["error"]) == (409, "track_unavailable")
    assert plex.calls[-1] == (f"{TRANSCODE}/stop", {"session": plex.decision()["session"]})


def test_a_subtitle_plex_would_send_beside_the_picture_is_refused(alice, plex):
    plex.subtitle_decision = "transcode"
    assert _start(alice, subtitle="32").status_code == 409


@pytest.mark.parametrize(
    "body",
    [{"quality": "original"}, {"quality": "1080p-20000"}, {"audio": "x"}, {"subtitle": "../1"}],
)
def test_a_malformed_start_is_refused(alice, plex, body):
    assert _start(alice, **body).status_code == 422
    assert plex.calls == []


@pytest.mark.parametrize("rating_key", ["301", "999"])
def test_only_titles_of_the_scanned_libraries_start(alice, plex, rating_key):
    assert _start(alice, rating_key).status_code == 404
    assert plex.calls == []


def test_a_tvs_stream_counts_towards_the_persons_limit(alice):
    for key in ("101", "102"):
        _cast(alice, key)
    alice.post("/api/watch/stream", json={"rating_key": "103", "quality": "best"})
    resp = _start(alice, "104")
    assert (resp.status_code, resp.json()["error"]) == (429, "too_many_streams")


def test_a_paused_tv_outlives_the_players_idle_time_and_a_players_stream_does_not(alice, tv, clock):
    url = _cast(alice)["url"]
    tv.get(url)
    other = alice.post("/api/watch/stream", json={"rating_key": "102", "quality": "best"})
    clock.now += IDLE_TIMEOUT_SECONDS + 1
    plex_stream.get_stream_manager().reap()
    assert tv.get(url).status_code == 200
    assert alice.get(other.json()["playlist"]).status_code == 410


def test_saving_progress_keeps_a_paused_tvs_stream(alice, tv, clock):
    started = _cast(alice)
    tv.get(started["url"])
    for _ in range(3):
        clock.now += TV_IDLE_TIMEOUT_SECONDS - 60
        saved = alice.post(
            "/api/watch/progress",
            json={"stream_id": started["stream_id"], "position_s": 60, "played_s": 60},
        )
        assert saved.status_code == 200
        plex_stream.get_stream_manager().reap()
    assert tv.get(started["url"]).status_code == 200


@pytest.mark.parametrize("address", [str, _segment_url], ids=["playlist", "segment"])
def test_a_plex_failure_tells_the_tv_nothing_of_plex_or_the_ticket(
    alice, tv, plex, caplog, address
):
    url = _cast(alice)["url"]
    plex.reads_fail = True
    with caplog.at_level(logging.DEBUG, logger="mediaman"):
        resp = tv.get(address(url))
    assert (resp.status_code, resp.json()) == (502, {"ok": False, "error": "unreachable"})
    assert resp.headers["access-control-allow-origin"] == "*"
    for secret in (_ticket(url), TOKEN, "plex-lan"):
        assert secret not in resp.text + caplog.text


def test_the_ticket_is_nowhere_in_the_database(alice, conn):
    ticket = _ticket(_cast(alice)["url"])
    assert ticket not in "\n".join(conn.iterdump())


ORIGIN = {"Origin": "http://testserver"}
TV_AGENT = {"User-Agent": "a TV"}


@pytest.fixture
def whole(app, conn, secret_key, monkeypatch):
    """The whole app, and a phone signed in with a session bound to it."""
    monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "strict")
    whole_app = create_app()
    whole_app.state.config = Config(secret_key=secret_key)
    whole_app.state.db = conn
    create_user(conn, "alice", "password1234", enforce_policy=False, role="viewer")
    phone = TestClient(whole_app, headers={"User-Agent": "a phone", **ORIGIN})
    session = create_session(conn, "alice", user_agent="a phone", client_ip="testclient")
    phone.cookies.set("session_token", session)
    return phone


def _with_cookie(phone, session: str) -> TestClient:
    client = TestClient(phone.app, headers=TV_AGENT, follow_redirects=False)
    client.cookies.set("session_token", session)
    return client


def _sessions(conn, user: str) -> int:
    sql = "SELECT COUNT(*) FROM admin_sessions WHERE username = ?"
    return conn.execute(sql, (user,)).fetchone()[0]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("address", [str, _segment_url], ids=["playlist", "segment"])
def test_a_tv_that_sends_the_phones_cookie_ends_neither_session_nor_ticket(
    whole, conn, method, address
):
    url = _cast(whole)["url"]
    tv = _with_cookie(whole, whole.cookies["session_token"])
    resp = tv.request(method, address(url))
    assert resp.status_code == (204 if method == "OPTIONS" else 200)
    assert _sessions(conn, "alice") == 1
    assert TestClient(whole.app).get(url).status_code == 200


@pytest.mark.parametrize("address", [str, _segment_url], ids=["playlist", "segment"])
def test_the_cookie_of_someone_who_must_change_their_password_does_not_hide_a_stream(
    whole, conn, address
):
    create_user(conn, "bob", "password1234", enforce_policy=False)
    bobs = create_session(conn, "bob")
    set_must_change_password(conn, "bob", True)
    url = _cast(whole)["url"]
    assert _with_cookie(whole, bobs).get(address(url)).status_code == 200


def test_a_made_up_ticket_with_the_phones_cookie_leaves_the_session(whole, conn):
    tv = _with_cookie(whole, whole.cookies["session_token"])
    made_up = f"/api/cast/{'x' * 43}/index.m3u8"
    assert tv.get(made_up).status_code == 404
    assert tv.get(_segment_url(made_up)).status_code == 404
    assert _sessions(conn, "alice") == 1


def test_starting_for_a_tv_still_checks_the_session(whole, conn):
    flagged = create_session(conn, "alice", user_agent="a phone", client_ip="testclient")
    set_must_change_password(conn, "alice", True)
    assert _start(whole).status_code == 403
    set_must_change_password(conn, "alice", False)
    assert _start(whole).status_code == 200
    stranger = _with_cookie(whole, flagged)
    stranger.headers.update(ORIGIN)
    assert _start(stranger).status_code == 401
    assert _sessions(conn, "alice") == 1


@pytest.mark.parametrize(
    "path",
    [
        "/api/cast/stream",
        "/api/cast/x/seg",
        "/api/cast/x/index.m3u8/",
        "/api/cast/x/../../watch/progress",
        "/api/cast/x/seg/../../../account/avatar",
        "/x/api/cast/x/index.m3u8",
    ],
)
def test_no_other_address_skips_the_check_of_the_cookie(whole, conn, path):
    _with_cookie(whole, whole.cookies["session_token"]).get(path)
    assert _sessions(conn, "alice") == 0


def test_another_method_on_a_ticket_route_does_not_skip_the_check_of_the_cookie(whole, conn):
    tv = _with_cookie(whole, whole.cookies["session_token"])
    tv.post(f"/api/cast/{'x' * 43}/index.m3u8", headers=ORIGIN)
    assert _sessions(conn, "alice") == 0


def test_an_ordinary_path_is_told_from_a_ticket_route_without_walking_the_routes(
    whole, monkeypatch
):
    def walked(routes):
        raise AssertionError("the routes were walked")

    monkeypatch.setattr(viewer_guard, "iter_route_contexts", walked)
    scope = {"type": "http", "method": "GET", "path": "/api/watch/search", "app": whole.app}
    assert not viewer_guard.is_ticket_route(Request(scope))


def test_a_ticket_route_is_known_by_its_decoded_path(whole, conn):
    url = _cast(whole)["url"]
    tv = _with_cookie(whole, whole.cookies["session_token"])
    assert tv.get(url.replace("cast", "c%61st")).status_code == 200
    assert _sessions(conn, "alice") == 1
