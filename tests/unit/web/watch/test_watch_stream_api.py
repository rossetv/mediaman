"""Tests for the Watch stream and progress API, against a fake Plex."""

from __future__ import annotations

import inspect
import json
import logging
import threading
from urllib.parse import urlsplit

import pytest
import requests
from fastapi.routing import iter_route_contexts

from mediaman.app_factory import _start_stream_reaper, create_app
from mediaman.config import Config
from mediaman.db import set_connection
from mediaman.services.media_meta import plex_stream
from mediaman.services.media_meta.plex_stream import StreamManager
from mediaman.services.media_meta.plex_watch import (
    PlexChapter,
    PlexItem,
    PlexItemNotFound,
    PlexMarker,
    PlexMedia,
    PlexUnavailable,
    PlexWatch,
)
from mediaman.web.repository.watch_progress import unmark_watched
from mediaman.web.routes.watch import stream_api

PLEX_HOST = "plex-lan.example.test"
PLEX_URL = f"http://{PLEX_HOST}:32400"
TOKEN = "tok-Zq81-only-in-tests"
TRANSCODE = "/video/:/transcode/universal"

CHILD_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:8\n"
    f"# served by {PLEX_URL}/?X-Plex-Token={TOKEN}\n"
    "#EXTINF:8.0,\n"
    "00000.ts\n"
    "#EXT-X-ENDLIST\n"
)

MEDIA = "<Video><Media><Part>%s</Part></Media></Video>"

PART_ID = "31415926"

NO_STORE = "no-store, no-transform"


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
    """Just enough of Plex's universal transcoder for a stream to start and play."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.unreachable = False
        self.video_decision = "copy"

    def request(self, method, url, *, params=None, headers=None, **kwargs):
        self.calls.append({"path": urlsplit(url).path, "params": dict(params or {})})
        if self.unreachable:
            raise requests.ConnectionError(f"cannot connect to {url}?X-Plex-Token={TOKEN}")
        path = urlsplit(url).path
        session = (params or {}).get("session")
        if path == f"{TRANSCODE}/start.m3u8":
            return FakeResponse(200, f"#EXTM3U\nsession/{session}/base/index.m3u8\n".encode())
        if path == f"{TRANSCODE}/decision":
            stream = f'<Stream streamType="1" decision="{self.video_decision}"/>'
            return FakeResponse(200, f"<MediaContainer>{MEDIA % stream}</MediaContainer>".encode())
        if path.endswith("/base/index.m3u8"):
            return FakeResponse(200, CHILD_PLAYLIST.encode())
        if path.endswith("/base/00000.ts"):
            return FakeResponse(200, b"video bytes")
        return FakeResponse(200, b"<MediaContainer/>")

    def session_ids(self) -> set[str]:
        return {c["params"]["session"] for c in self.calls if "session" in c["params"]}


def _item(key: str) -> PlexItem:
    """Plex items by key: 1xx movies, 2xx episodes of season 200 of show 199, 3xx elsewhere."""
    if key == "199":
        return PlexItem("show", key, "Show", "", None, 0, "1", None, False)
    if key.startswith("1"):
        return PlexItem("movie", key, "Film", "", 2020, 6000, "1", None, True)
    if key == "400":
        return PlexItem(
            "movie",
            key,
            "Detailed",
            "",
            2020,
            6000,
            "1",
            None,
            False,
            media=PlexMedia(1920, 800, "1080", 11000, True, PART_ID, "987654321", True),
            markers=(PlexMarker("intro", 5.0, 65.5, False),),
            chapters=(PlexChapter("One", 0.0, 600.0),),
        )
    if key.startswith("2"):
        return PlexItem(
            "episode",
            key,
            "Episode",
            "",
            None,
            1500,
            "1",
            None,
            False,
            parent_rating_key="200",
            grandparent_rating_key="199",
        )
    if key.startswith("3"):
        return PlexItem("movie", key, "Elsewhere", "", 2020, 6000, "9", None, False)
    if key.startswith("5"):
        raise PlexUnavailable("Plex could not be reached")
    raise PlexItemNotFound("Item not found in Plex")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

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
        lambda url, **kwargs: (True, PLEX_HOST, ("203.0.113.5",)),
    )
    monkeypatch.setattr(
        plex_stream,
        "_manager",
        StreamManager(lambda: (PLEX_URL, TOKEN), session=fake, clock=clock),
    )
    monkeypatch.setattr(PlexWatch, "get_item", lambda self, key, marks: _item(key))
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
    return app_factory(stream_api.router, conn=conn)


@pytest.fixture
def alice(app, conn, authed_client):
    return authed_client(app, conn, username="alice")


@pytest.fixture
def bob(app, conn, authed_client):
    return authed_client(app, conn, username="bob")


def _start(client, rating_key: str = "101", quality: str = "best"):
    return client.post("/api/watch/stream", json={"rating_key": rating_key, "quality": quality})


def _stream(client, rating_key: str = "101") -> str:
    resp = _start(client, rating_key)
    assert resp.status_code == 200, resp.text
    return resp.json()["stream_id"]


def _progress_rows(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM watch_progress").fetchall()
    return [dict(r) for r in rows]


def test_started_stream_plays_through_mediaman_with_no_store_headers(alice, plex):
    resp = _start(alice)
    assert resp.status_code == 200
    stream_id = resp.json()["stream_id"]
    assert resp.json()["playlist"] == f"/api/watch/stream/{stream_id}/index.m3u8"
    # The item's selected subtitle is burned in.
    assert {c["params"].get("subtitles") for c in plex.calls} >= {"burn"}

    playlist = alice.get(resp.json()["playlist"])
    assert playlist.status_code == 200
    assert playlist.headers["content-type"] == "application/vnd.apple.mpegurl"
    assert f"/api/watch/stream/{stream_id}/seg/00000.ts" in playlist.text.splitlines()

    segment = alice.get(f"/api/watch/stream/{stream_id}/seg/00000.ts")
    assert segment.status_code == 200
    assert segment.content == b"video bytes"
    assert segment.headers["content-type"] == "video/mp2t"
    for r in (playlist, segment):
        assert r.headers["cache-control"] == NO_STORE
        assert r.headers["x-accel-buffering"] == "no"


@pytest.mark.parametrize(
    "rating_key",
    ["301", "199", "999"],
    ids=["outside the scanned libraries", "a show", "unknown to plex"],
)
def test_start_refuses_anything_but_a_movie_or_episode_in_the_scanned_libraries(
    alice, plex, rating_key
):
    resp = _start(alice, rating_key)
    assert resp.status_code == 404
    assert plex.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"rating_key": "101", "quality": "ultra"},
        {"rating_key": "../101", "quality": "best"},
        {"rating_key": "101"},
    ],
    ids=["unknown quality", "non-numeric key", "no quality"],
)
def test_start_refuses_a_malformed_request(alice, plex, body):
    assert alice.post("/api/watch/stream", json=body).status_code == 422
    assert plex.calls == []


def test_start_accepts_a_numeric_rating_key(alice):
    resp = alice.post("/api/watch/stream", json={"rating_key": 101, "quality": "low"})
    assert resp.status_code == 200


def test_start_answers_429_past_the_stream_limit(alice):
    for key in ("101", "102", "103"):
        _stream(alice, key)
    resp = _start(alice, "104")
    assert resp.status_code == 429
    assert resp.json()["error"] == "too_many_streams"


def test_start_says_not_set_up_when_plex_has_no_token(alice, conn, plex):
    conn.execute("DELETE FROM settings WHERE key = 'plex_token'")
    conn.commit()
    resp = _start(alice)
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_set_up"


@pytest.mark.parametrize("where", ["item lookup", "transcoder"])
def test_start_says_unreachable_when_plex_fails(alice, plex, where):
    if where == "transcoder":
        plex.unreachable = True
    resp = _start(alice, "501" if where == "item lookup" else "101")
    assert resp.status_code == 502
    assert resp.json()["error"] == "unreachable"


def test_another_users_stream_is_gone_exactly_like_an_unknown_one(alice, bob):
    stream_id = _stream(alice)
    unknown = bob.get("/api/watch/stream/no-such-stream/index.m3u8")
    assert unknown.status_code == 410
    for resp in (
        bob.get(f"/api/watch/stream/{stream_id}/index.m3u8"),
        bob.get(f"/api/watch/stream/{stream_id}/seg/00000.ts"),
        bob.post(
            "/api/watch/progress",
            json={"stream_id": stream_id, "position_s": 10, "played_s": 10},
        ),
    ):
        assert resp.status_code == 410
        assert resp.content == unknown.content
        assert resp.headers["cache-control"] == NO_STORE
    assert alice.get(f"/api/watch/stream/{stream_id}/index.m3u8").status_code == 200


def test_bad_segment_name_is_404(alice):
    stream_id = _stream(alice)
    assert alice.get(f"/api/watch/stream/{stream_id}/seg/index.m3u8").status_code == 404


def test_stop_is_quiet_when_repeated_and_never_stops_someone_elses_stream(alice, bob):
    stream_id = _stream(alice)
    assert bob.post(f"/api/watch/stream/{stream_id}/stop").status_code == 204
    assert alice.get(f"/api/watch/stream/{stream_id}/index.m3u8").status_code == 200

    assert alice.post(f"/api/watch/stream/{stream_id}/stop").status_code == 204
    assert alice.post(f"/api/watch/stream/{stream_id}/stop").status_code == 204
    assert alice.get(f"/api/watch/stream/{stream_id}/index.m3u8").status_code == 410


def test_progress_is_saved_for_the_owner_with_item_season_and_duration_from_the_stream(
    alice, conn, clock
):
    stream_id = _stream(alice, "201")
    clock.now += 400
    resp = alice.post(
        "/api/watch/progress",
        json={"stream_id": stream_id, "position_s": 1400, "played_s": 400},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "watched": True}
    [row] = _progress_rows(conn)
    assert row["username"] == "alice"
    assert row["rating_key"] == "201"
    assert row["media_item_id"] == "200"
    assert row["show_rating_key"] == "199"
    assert row["duration_s"] == 1500
    assert row["position_s"] == 1400
    assert row["credited_at"] is not None


def _post_played(client, stream_id: str, played_s: float):
    return client.post(
        "/api/watch/progress",
        json={"stream_id": stream_id, "position_s": 500, "played_s": played_s},
    )


def test_a_huge_played_time_sent_at_once_earns_no_credit(alice, conn):
    stream_id = _stream(alice)
    assert _post_played(alice, stream_id, 1e9).status_code == 200
    [row] = _progress_rows(conn)
    assert row["credited_at"] is None


def test_played_time_earned_before_a_restart_still_earns_credit(alice, conn, clock):
    first = _stream(alice)
    clock.now += 280
    _post_played(alice, first, 280)
    second = _stream(alice)  # the player restarts on 410 at its position
    clock.now += 20
    _post_played(alice, second, 300)
    [row] = _progress_rows(conn)
    assert row["credited_at"] is not None


@pytest.mark.parametrize("content_type", ["application/json", "text/plain;charset=UTF-8"])
def test_progress_accepts_a_beacon_body(alice, conn, content_type):
    stream_id = _stream(alice)
    body = json.dumps({"stream_id": stream_id, "position_s": 60.5, "played_s": 60})
    resp = alice.post("/api/watch/progress", content=body, headers={"Content-Type": content_type})
    assert resp.status_code == 200
    assert resp.json()["watched"] is False
    [row] = _progress_rows(conn)
    assert (row["rating_key"], row["media_item_id"], row["position_s"]) == ("101", "101", 60.5)


def test_progress_on_another_users_stream_saves_nothing(alice, bob, conn):
    stream_id = _stream(alice)
    resp = bob.post(
        "/api/watch/progress",
        json={"stream_id": stream_id, "position_s": 5000, "played_s": 5000},
    )
    assert resp.status_code == 410
    assert _progress_rows(conn) == []


def test_stop_with_a_final_position_saves_it_then_stops(alice, conn, clock):
    stream_id = _stream(alice, "201")
    clock.now += 400
    body = json.dumps({"position_s": 1400, "played_s": 400})
    resp = alice.post(
        f"/api/watch/stream/{stream_id}/stop",
        content=body,
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert resp.status_code == 204
    [row] = _progress_rows(conn)
    assert (row["rating_key"], row["position_s"], row["duration_s"]) == ("201", 1400, 1500)
    assert row["credited_at"] is not None
    assert alice.get(f"/api/watch/stream/{stream_id}/index.m3u8").status_code == 410


_EARLIER = "2020-01-01T00:00:00+00:00"


def _saved_earlier(client, conn, clock, stream_id: str, position_s: float = 900.5) -> None:
    clock.now += 100
    place = {"stream_id": stream_id, "position_s": position_s, "played_s": 100}
    assert client.post("/api/watch/progress", json=place).status_code == 200
    conn.execute("UPDATE watch_progress SET updated_at = ?", (_EARLIER,))
    conn.commit()


@pytest.mark.parametrize("ended", ["live", "reaped", "restarted"])
def test_leaving_a_paused_player_does_not_count_as_playing(alice, conn, clock, ended):
    stream_id = _stream(alice, "201")
    _saved_earlier(alice, conn, clock, stream_id)
    if ended == "reaped":
        _reap_after_idle(clock)
    elif ended == "restarted":
        stream_id = _stream(alice, "201")

    alice.post(f"/api/watch/stream/{stream_id}/stop", json={"position_s": 900.5, "played_s": 100})

    [row] = _progress_rows(conn)
    assert row["updated_at"] == _EARLIER


def test_leaving_a_paused_player_keeps_a_mark_made_meanwhile(alice, conn, clock):
    stream_id = _stream(alice, "201")
    _saved_earlier(alice, conn, clock, stream_id)
    conn.execute("UPDATE watch_progress SET watched = 1")
    conn.commit()

    alice.post(f"/api/watch/stream/{stream_id}/stop", json={"position_s": 900.5, "played_s": 100})

    assert _progress_rows(conn)[0]["watched"] == 1


@pytest.mark.parametrize("ended", ["live", "reaped"])
def test_leaving_a_paused_player_keeps_a_not_watched_mark_made_meanwhile(alice, conn, clock, ended):
    stream_id = _stream(alice, "201")
    _saved_earlier(alice, conn, clock, stream_id, 1450.5)
    unmark_watched(conn, "alice", "episode", "201")
    if ended == "reaped":
        _reap_after_idle(clock)

    alice.post(f"/api/watch/stream/{stream_id}/stop", json={"position_s": 1450.5, "played_s": 100})

    [row] = _progress_rows(conn)
    assert (row["watched"], row["position_s"], row["seen_at"]) == (0, 0, None)


@pytest.mark.parametrize(
    ("position", "played"), [(950, 100), (900.5, 160)], ids=["moved", "played back to it"]
)
def test_a_final_stop_after_moving_or_playing_is_saved(alice, conn, clock, position, played):
    stream_id = _stream(alice, "201")
    _saved_earlier(alice, conn, clock, stream_id)
    clock.now += 60

    alice.post(
        f"/api/watch/stream/{stream_id}/stop", json={"position_s": position, "played_s": played}
    )

    [row] = _progress_rows(conn)
    assert (row["position_s"], row["updated_at"] > _EARLIER) == (position, True)


def test_stop_clamps_the_played_time_like_progress(alice, conn):
    stream_id = _stream(alice)
    alice.post(f"/api/watch/stream/{stream_id}/stop", json={"position_s": 500, "played_s": 1e9})
    [row] = _progress_rows(conn)
    assert row["credited_at"] is None


def test_stop_by_another_user_saves_nothing_and_leaves_the_stream(alice, bob, conn):
    stream_id = _stream(alice)
    resp = bob.post(
        f"/api/watch/stream/{stream_id}/stop", json={"position_s": 5000, "played_s": 5000}
    )
    assert resp.status_code == 204
    assert _progress_rows(conn) == []
    assert alice.get(f"/api/watch/stream/{stream_id}/index.m3u8").status_code == 200


def test_stop_with_a_bad_body_still_stops(alice, conn):
    stream_id = _stream(alice)
    resp = alice.post(f"/api/watch/stream/{stream_id}/stop", content="not json")
    assert resp.status_code == 204
    assert _progress_rows(conn) == []
    assert alice.get(f"/api/watch/stream/{stream_id}/index.m3u8").status_code == 410


def _reap_after_idle(clock) -> None:
    clock.now += plex_stream.IDLE_TIMEOUT_SECONDS + 1
    assert plex_stream.get_stream_manager().reap() == 1


def test_a_stop_arriving_after_the_reaper_still_saves_the_final_position(alice, conn, clock):
    stream_id = _stream(alice, "201")
    clock.now += 400
    assert alice.get(f"/api/watch/stream/{stream_id}/index.m3u8").status_code == 200
    _reap_after_idle(clock)
    resp = alice.post(
        f"/api/watch/stream/{stream_id}/stop", json={"position_s": 1400, "played_s": 400}
    )
    assert resp.status_code == 204
    [row] = _progress_rows(conn)
    assert (row["username"], row["rating_key"], row["position_s"]) == ("alice", "201", 1400)
    assert row["credited_at"] is not None


def test_a_progress_beacon_after_the_reaper_saves_and_still_says_gone(alice, conn, clock):
    stream_id = _stream(alice)
    _reap_after_idle(clock)
    resp = alice.post(
        "/api/watch/progress", json={"stream_id": stream_id, "position_s": 60, "played_s": 60}
    )
    assert resp.status_code == 410
    [row] = _progress_rows(conn)
    assert (row["rating_key"], row["position_s"]) == ("101", 60)


def test_a_reaped_stream_credits_no_played_time_past_its_last_use(alice, conn, clock):
    stream_id = _stream(alice)
    clock.now += 600
    plex_stream.get_stream_manager().reap()
    alice.post(f"/api/watch/stream/{stream_id}/stop", json={"position_s": 600, "played_s": 600})
    [row] = _progress_rows(conn)
    assert row["credited_at"] is None


def test_another_user_cannot_save_into_a_reaped_stream(alice, bob, conn, clock):
    stream_id = _stream(alice)
    _reap_after_idle(clock)
    bob.post(f"/api/watch/stream/{stream_id}/stop", json={"position_s": 500, "played_s": 500})
    bob.post(
        "/api/watch/progress", json={"stream_id": stream_id, "position_s": 500, "played_s": 500}
    )
    assert _progress_rows(conn) == []


def test_a_late_save_from_a_reaped_stream_never_overwrites_a_newer_stream_of_the_same_item(
    alice, conn, clock
):
    old_id = _stream(alice)
    _reap_after_idle(clock)
    new_id = _stream(alice)
    alice.post("/api/watch/progress", json={"stream_id": new_id, "position_s": 900, "played_s": 0})
    alice.post("/api/watch/progress", json={"stream_id": old_id, "position_s": 60, "played_s": 0})
    alice.post(f"/api/watch/stream/{old_id}/stop", json={"position_s": 60, "played_s": 0})
    [row] = _progress_rows(conn)
    assert row["position_s"] == 900


def test_a_stream_reaped_long_ago_saves_nothing(alice, conn, clock):
    stream_id = _stream(alice)
    _reap_after_idle(clock)
    clock.now += plex_stream.REAPED_KEEP_SECONDS + 1
    plex_stream.get_stream_manager().reap()
    alice.post(f"/api/watch/stream/{stream_id}/stop", json={"position_s": 60, "played_s": 60})
    assert _progress_rows(conn) == []


def test_no_response_or_log_line_reveals_plex_address_session_or_token(alice, bob, plex, caplog):
    caplog.set_level(logging.DEBUG)
    stream_id = _stream(alice)
    responses = [
        alice.get(f"/api/watch/stream/{stream_id}/index.m3u8"),
        alice.get(f"/api/watch/stream/{stream_id}/seg/00000.ts"),
        alice.post(
            "/api/watch/progress",
            json={"stream_id": stream_id, "position_s": 1, "played_s": 1},
        ),
        bob.get(f"/api/watch/stream/{stream_id}/index.m3u8"),
    ]
    plex.unreachable = True
    responses += [
        alice.get(f"/api/watch/stream/{stream_id}/index.m3u8"),
        alice.get(f"/api/watch/stream/{stream_id}/seg/00000.ts"),
        _start(alice, "102"),
        alice.post(f"/api/watch/stream/{stream_id}/stop"),
    ]
    assert [r.status_code for r in responses[4:7]] == [502, 502, 502]
    secrets = {TOKEN, PLEX_HOST, "32400", *plex.session_ids()}
    for resp in responses:
        seen = resp.text + json.dumps(dict(resp.headers))
        assert not [s for s in secrets if s in seen], resp.request.url
    assert not [s for s in secrets if s in caplog.text]


def test_reaper_keeps_running_after_a_failed_round_and_stops_on_shutdown():
    rounds: list[int] = []
    second_round = threading.Event()

    def reap() -> int:
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("one bad round")
        second_round.set()
        return 0

    shutdown = _start_stream_reaper(reap, 0.001)
    assert second_round.wait(timeout=5)
    shutdown()
    assert not [t for t in threading.enumerate() if t.name == "watch-stream-reaper"]


def test_the_real_app_serves_watch_stream_and_account_routes():
    routes = {
        (method, route.path_format)
        for route in iter_route_contexts(create_app().routes)
        for method in route.methods or {"GET"}
    }
    assert {
        ("GET", "/watch"),
        ("GET", "/account"),
        ("POST", "/api/watch/stream"),
        ("GET", "/api/watch/stream/{stream_id}/index.m3u8"),
        ("GET", "/api/watch/stream/{stream_id}/seg/{name}"),
        ("POST", "/api/watch/stream/{stream_id}/stop"),
        ("POST", "/api/watch/progress"),
    } <= routes


def test_stop_runs_off_the_event_loop():
    """Stop does blocking Plex and SQLite work, so it must be a threadpool (plain def) handler."""
    assert not inspect.iscoroutinefunction(stream_api.stop_stream)


def test_a_viewers_beacon_reaches_progress_through_the_real_app(
    conn, secret_key, viewer_client, plex
):
    set_connection(conn)
    application = create_app()
    application.state.config = Config(secret_key=secret_key)
    application.state.db = conn
    client = viewer_client(application, conn)
    resp = client.post(
        "/api/watch/progress",
        content=json.dumps({"stream_id": "gone-stream", "position_s": 1, "played_s": 1}),
        headers={"Content-Type": "text/plain;charset=UTF-8", "Origin": "http://testserver"},
    )
    assert resp.status_code == 410


def test_start_says_what_the_title_offers(alice):
    body = _start(alice, "400", "auto").json()

    assert body["quality"] == "1080p-8000"
    assert [r["id"] for r in body["qualities"]][:3] == ["auto", "original", "1080p-10000"]
    assert body["markers"] == [{"kind": "intro", "start_s": 5.0, "end_s": 65.5, "final": False}]
    assert body["chapters"] == [{"title": "One", "start_s": 0.0, "end_s": 600.0}]
    assert body["previews"] == {"interval_s": 5, "count": 1200}


def test_start_of_a_title_without_details_offers_the_basic_three_and_nothing_else(alice):
    body = _start(alice).json()

    assert body["quality"] == "best"
    assert [r["id"] for r in body["qualities"]] == ["best", "medium", "low"]
    assert (body["markers"], body["chapters"], body["previews"]) == ([], [], None)


def test_the_start_answer_holds_nothing_of_plex(alice, plex):
    text = _start(alice, "400").text

    for secret in (PART_ID, "987654321", "library", TOKEN, PLEX_HOST, *plex.session_ids()):
        assert secret not in text


def test_an_original_plex_stops_copying_plays_the_best_conversion_and_is_taken_off(alice, plex):
    assert _start(alice, "400", "original").json()["quality"] == "original"
    plex.video_decision = "transcode"

    body = _start(alice, "400", "original").json()

    assert body["quality"] == "1080p-10000"
    assert "original" not in [r["id"] for r in body["qualities"]]


def test_original_is_not_offered_when_plex_would_convert_it(alice, plex):
    plex.video_decision = "transcode"

    body = _start(alice, "400", "720p-4000").json()

    assert body["quality"] == "720p-4000"
    assert "original" not in [r["id"] for r in body["qualities"]]


def test_the_stream_remembers_where_its_preview_frames_are(alice, plex):
    stream_id = _start(alice, "400").json()["stream_id"]

    info = plex_stream.get_stream_manager().peek(stream_id, "alice").info
    assert info["preview"] == {"part_id": PART_ID, "part_size": "987654321", "count": 1200}


@pytest.mark.parametrize(
    "quality", [*plex_stream.QUALITY_PRESETS, "auto", "original"], ids=lambda q: q
)
def test_start_accepts_every_known_quality(alice, quality):
    assert _start(alice, "400", quality).status_code == 200
