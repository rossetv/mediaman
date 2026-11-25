"""Tests for playing on an older TV: who may, what the remote does to it, what ends it."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import get_db, set_connection
from mediaman.main import create_app
from mediaman.services.media_meta import plex_stream
from mediaman.services.media_meta.plex_watch import PlexWatch
from mediaman.services.tv import older, players
from mediaman.services.tv.older import Claims
from mediaman.services.tv.players import HOLD_SECONDS
from mediaman.web.auth.password_hash import delete_user, set_must_change_password
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.auth.session_store import create_session, destroy_session
from mediaman.web.repository import older_tvs as repo
from mediaman.web.repository.watch_progress import get_progress
from mediaman.web.routes import older_play, older_reach, older_stream
from mediaman.web.routes import tv as tv_routes
from tests.helpers.factories import insert_settings
from tests.unit.services.dlna.fake_tv import DESCRIPTION, TV, FakeTv, MovedTv, http, soap
from tests.unit.web.test_cast import ORIGIN, Clock, FakeResponse
from tests.unit.web.test_older_stream import Reader
from tests.unit.web.test_tv import (  # noqa: F401
    _command,
    _lives,
    _play,
    _tv_of,
    app,
    clock,
    lookups,
    manager,
    plex,
)

HOST = "http://192.168.1.10:8282"
PLAYLIST = (
    "#EXTM3U\n#EXT-X-TARGETDURATION:60\n"
    + "".join(f"#EXTINF:60.0,\n{n:05}.ts\n" for n in range(100))
    + "#EXT-X-ENDLIST\n"
)


class Home:
    """The TV as it answers, and the driver that plays on it, stepped by hand."""

    def __init__(self, clock: Clock) -> None:  # noqa: F811
        self.clock = clock
        self.tv = FakeTv()
        self.drivers: list[older.Driver] = []
        self.status("STOPPED")

    def status(self, state: str, count: int = 0) -> None:
        at = f"{count // 3600}:{count // 60 % 60:02}:{count % 60:02}"
        self.tv.actions["GetTransportInfo"] = soap(
            "GetTransportInfo", f"<CurrentTransportState>{state}</CurrentTransportState>"
        )
        self.tv.actions["GetPositionInfo"] = soap("GetPositionInfo", f"<RelTime>{at}</RelTime>")
        self.tv.actions["GetVolume"] = soap("GetVolume", "<CurrentVolume>40</CurrentVolume>")

    def drive(self, driver: older.Driver) -> None:
        driver._clock = driver._claims_clock = self.clock
        driver._began = self.clock()
        driver._start_work = lambda do: do()
        self.drivers.append(driver)

    def step(self, seconds: float = 1.0, driver: int = -1) -> bool:
        self.clock.now += seconds
        going = self.drivers[driver].step()
        if not going:
            self.drivers[driver]._let_go()
        return going

    def playing(self, count: int = 0) -> None:
        """Let the TV take the film and play it."""
        self.step()
        self.status("PLAYING", count)
        self.step()

    def urls(self) -> list[str]:
        found = [re.search(rb"<CurrentURI>(.*?)</CurrentURI>", r) for r in self.tv.requests]
        return [m[1].decode() for m in found if m]

    def did(self, action: str) -> int:
        return sum(f'#{action}"'.encode() in r for r in self.tv.requests)


@pytest.fixture
def whole(app, conn, secret_key, plex, monkeypatch):  # noqa: F811
    real = plex.request

    def request(method, url, **kwargs):
        if url.endswith("/base/index.m3u8"):
            return FakeResponse(200, PLAYLIST.encode())
        return real(method, url, **kwargs)

    monkeypatch.setattr(plex, "request", request)
    monkeypatch.setattr(older, "_claims", Claims())
    monkeypatch.setattr(older_stream, "_feeds", {})
    made = create_app()
    made.state.config = Config(secret_key=secret_key)
    made.state.db = conn
    set_connection(conn)
    insert_settings(conn, older_tvs_enabled="true", older_tvs_http="true", tv_address=HOST)
    conn.execute(
        "INSERT INTO tvs (kind, name, address, udn, created_at)"
        " VALUES ('older', 'Kitchen TV', ?, 'uuid:1234-abcd', '2026-01-01')",
        (TV,),
    )
    conn.commit()
    return made


@pytest.fixture
def home(whole, clock, monkeypatch) -> Home:  # noqa: F811
    made = Home(clock)
    monkeypatch.setattr(older_reach, "Wire", made.tv.wire)
    monkeypatch.setattr(older_play, "_drive", made.drive)
    return made


@pytest.fixture
def kitchen(conn, home) -> int:
    return repo.every(conn)[0].id


@pytest.fixture
def person(whole, conn):
    def sign_in(name: str) -> TestClient:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at, role)"
            " VALUES (?, 'x', '2026-01-01', 'viewer')",
            (name,),
        )
        conn.commit()
        client = TestClient(whole, headers={"User-Agent": "a phone", **ORIGIN})
        session = create_session(conn, name, user_agent="a phone", client_ip="testclient")
        client.cookies.set("session_token", session)
        return client

    return sign_in


@pytest.fixture
def amy(person):
    return person("amy")


@pytest.fixture
def bob(person):
    return person("bob")


@pytest.fixture
def set_up(whole):
    return TestClient(whole)


def _streams() -> list[str]:
    return list(plex_stream.get_stream_manager()._streams)


def _path(home) -> str:
    return home.urls()[-1].removeprefix(HOST)


def test_a_viewer_plays_on_an_admins_tv_and_it_fetches_the_film_without_a_session(
    amy, home, kitchen, set_up
):
    resp = _play(amy, kitchen, position_s=125)
    assert resp.status_code == 200, resp.text
    home.playing()
    (url,) = home.urls()
    assert re.fullmatch(rf"{HOST}/api/cast/[A-Za-z0-9_-]{{43}}/from/2\.ts", url)
    assert set_up.get(_path(home)).status_code == 200
    home.status("PLAYING", 60)
    home.step(60)
    tv = _tv_of(amy, kitchen)
    assert (tv["kind"], tv["on"], tv["can_volume"]) == ("older", True, True)
    assert (tv["play"]["state"], tv["play"]["position_s"]) == ("playing", 180)
    assert "url" not in tv["play"]
    assert url.split("/")[5] not in amy.get("/api/tvs").text


@pytest.mark.parametrize("hangs_up", [True, False])
def test_behind_the_whole_app_a_tv_that_reads_no_more_lets_go_of_the_film(
    amy, home, kitchen, whole, monkeypatch, hangs_up
):
    monkeypatch.setattr(older_stream, "_LIVES_SECONDS", 0.02)
    _play(amy, kitchen)
    home.step()
    (stream,) = _streams()
    feed = older_stream._feed(stream)

    async def run():
        reader = await Reader(whole, _path(home)).open().reads()
        assert len(feed.readers) == 1
        if hangs_up:
            reader.hung_up.set()
        else:
            plex_stream.get_stream_manager().end(stream, "amy")
        await reader.until(lambda: not feed.readers)
        reader.hung_up.set()
        reader.takes.set()
        await reader.is_over()

    asyncio.run(run())


def test_the_remote_steers_it_as_any_tv(amy, home, kitchen):
    _play(amy, kitchen)
    home.playing()
    for kind, body, action in (
        ("pause", {}, "Pause"),
        ("play", {}, "Play"),
        ("volume", {"level": 0.5}, "SetVolume"),
    ):
        before = home.did(action)
        assert _command(amy, kitchen, kind, **body).status_code == 200
        home.step()
        assert home.did(action) == before + 1, kind
    _command(amy, kitchen, "seek", position_s=3000)
    home.step()
    assert home.urls()[-1].endswith("/from/50.ts")
    _command(amy, kitchen, "subtitles", subtitle="31")
    home.step()
    assert home.urls()[-1] != home.urls()[-2]
    stopped = _command(amy, kitchen, "stop").json()["tv"]["last"]
    assert stopped["reason"] == "stopped"
    before = home.did("Stop")
    assert home.step() is False
    assert home.did("Stop") == before + 1


def test_the_next_episode_follows_by_itself(amy, home, kitchen, set_up, plex):  # noqa: F811
    _play(amy, kitchen, "501", position_s=2860)
    home.playing(55)
    _has_every_segment(plex)
    set_up.get(_path(home))
    home.status("STOPPED", 55)
    home.step()
    assert _tv_of(amy, kitchen)["play"]["state"] == "upnext"
    for _ in range(12):
        home.step()
    assert _tv_of(amy, kitchen)["play"]["rating_key"] == "502"
    assert home.urls()[-1].endswith("/from/0.ts")


def _has_every_segment(plex):  # noqa: F811
    real = plex.request

    def request(method, url, **kwargs):
        return FakeResponse(200, b"x") if url.endswith(".ts") else real(method, url, **kwargs)

    plex.request = request


def test_a_seek_within_the_segment_it_is_in_is_a_film_not_sent_yet(
    amy,
    home,
    kitchen,
    set_up,
    plex,  # noqa: F811
):
    _play(amy, kitchen)
    home.playing()
    _has_every_segment(plex)
    set_up.get(_path(home))
    (stream,) = _streams()
    assert older_stream.sent(stream) == (True, False)
    _command(amy, kitchen, "seek", position_s=1)
    home.step()
    assert home.urls()[-1] == home.urls()[-2]
    assert older_stream.sent(stream) == (False, False)


def test_the_place_is_kept_with_where_the_credits_start(
    amy,
    home,
    kitchen,
    conn,
    monkeypatch,
    clock,  # noqa: F811
):
    from mediaman.web.routes import cast

    monkeypatch.setattr(
        cast,
        "_progress_info",
        lambda item, real=cast._progress_info: real(item) | {"credits_s": 5000},
    )
    _play(amy, kitchen)
    home.playing()
    for at in (3000, 5100):
        home.status("PLAYING", at)
        home.step(60)
    _command(amy, kitchen, "stop")
    saved = get_progress(conn, "amy", ["101"])["101"]
    assert saved.watched


def test_it_goes_on_from_the_saved_place(amy, home, kitchen, conn):
    _play(amy, kitchen, position_s=600)
    home.playing()
    home.status("PLAYING", 120)
    home.step(120)
    _command(amy, kitchen, "stop")
    home.step()
    assert get_progress(conn, "amy", ["101"])["101"].position_s == 720
    _play(amy, kitchen)
    home.step()
    assert home.urls()[-1].endswith("/from/12.ts")


def test_a_paused_tv_keeps_its_stream(amy, home, kitchen, manager, clock):  # noqa: F811
    _play(amy, kitchen)
    home.playing()
    (stream,) = _streams()
    home.status("PAUSED_PLAYBACK")
    for _ in range(70):
        clock.now += 59
        home.step()
        manager.reap()
    assert _lives(manager, stream)


def test_another_person_is_asked_before_they_take_it_over(amy, bob, home, kitchen, manager):  # noqa: F811
    _play(amy, kitchen)
    home.playing()
    (stream,) = _streams()
    first = _path(home)
    asked = _play(bob, kitchen)
    assert (asked.status_code, asked.json()["error"]) == (409, "in_use")
    assert _lives(manager, stream)
    seen = next(t for t in bob.get("/api/tvs").json()["tvs"] if t["id"] == kitchen)
    assert (seen["busy"], seen["play"]) == (True, None)
    assert _command(bob, kitchen, "stop").json()["tv"]["play"] is None
    assert _lives(manager, stream)

    assert _play(bob, kitchen, confirm=True).status_code == 200
    assert not _lives(manager, stream)
    stops = home.did("Stop")
    assert home.step(driver=0) is False
    assert home.did("Stop") == stops
    told = amy.get(f"/api/tvs/{kitchen}")
    assert (told.status_code, told.json()["error"]) == (410, "taken")
    assert amy.get(first).status_code == 404
    home.step()
    assert _tv_of(bob, kitchen)["play"]["state"] == "loading"
    # And back, from a phone that was told it lost the TV.
    assert _play(amy, kitchen, confirm=True).status_code == 200


def _plex_fails(amy, plex, monkeypatch):  # noqa: F811
    real = plex.request

    def request(method, url, **kwargs):
        return FakeResponse(500) if url.endswith("/decision") else real(method, url, **kwargs)

    monkeypatch.setattr(plex, "request", request)


def _no_such_title(amy, plex, monkeypatch):  # noqa: F811
    real = PlexWatch._metadata

    def metadata(self, key, query=""):
        return real(self, "999" if key == "102" else key, query)

    monkeypatch.setattr(PlexWatch, "_metadata", metadata)


def _three_screens(amy, plex, monkeypatch):  # noqa: F811
    for key in ("111", "112", "113"):
        amy.post("/api/cast/stream", json={"rating_key": key, "quality": "auto"})


@pytest.mark.parametrize(
    ("refuse", "status"), [(_plex_fails, 502), (_no_such_title, 404), (_three_screens, 429)]
)
def test_a_refused_takeover_leaves_the_film_that_is_on(
    amy,
    bob,
    home,
    kitchen,
    plex,  # noqa: F811
    monkeypatch,
    manager,  # noqa: F811
    refuse,
    status,
):
    _play(bob, kitchen)
    home.playing()
    (stream,) = _streams()
    refuse(amy, plex, monkeypatch)
    assert _play(amy, kitchen, "102", confirm=True).status_code == status
    assert _lives(manager, stream, "bob")
    stops = home.did("Stop")
    assert home.step(driver=0)
    assert home.did("Stop") == stops
    assert _tv_of(bob, kitchen)["play"]["state"] == "playing"
    assert len(home.drivers) == 1


@pytest.mark.parametrize(("refuse", "status"), [(_plex_fails, 502), (_no_such_title, 404)])
def test_a_refused_play_leaves_the_persons_own_film_on(
    amy,
    home,
    kitchen,
    plex,  # noqa: F811
    monkeypatch,
    refuse,
    status,
):
    _play(amy, kitchen)
    home.playing()
    refuse(amy, plex, monkeypatch)
    assert _play(amy, kitchen, "102").status_code == status
    assert home.step()
    play = _tv_of(amy, kitchen)["play"]
    assert (play["rating_key"], play["state"]) == ("101", "playing")


def test_a_stream_that_started_for_a_tv_it_cannot_play_on_is_given_back(
    amy, home, kitchen, monkeypatch
):
    def off(self, *args, **kwargs):
        raise players.TvOff("x")

    monkeypatch.setattr(players.Players, "begin", off)
    assert _play(amy, kitchen).status_code == 409
    assert _streams() == []


def test_a_play_that_fails_once_the_tv_is_taken_stops_the_tv(
    amy,
    home,
    kitchen,
    plex,  # noqa: F811
    monkeypatch,
):
    _play(amy, kitchen)
    home.playing()
    _plex_fails(amy, plex, monkeypatch)
    assert _play(amy, kitchen).status_code == 502
    stops = home.did("Stop")
    home.step(driver=0)
    home.step()
    assert home.did("Stop") == stops + 1
    assert _tv_of(amy, kitchen)["play"]["state"] == "failed"


def test_a_persons_own_film_on_the_tv_makes_room_for_the_next(amy, home, kitchen):
    _play(amy, kitchen)
    home.playing()
    for key in ("111", "112"):
        amy.post("/api/cast/stream", json={"rating_key": key, "quality": "auto"})
    assert _play(amy, kitchen, "102").status_code == 200
    home.step()
    assert _tv_of(amy, kitchen)["play"]["rating_key"] == "102"
    assert len(_streams()) == 3


def test_the_same_title_asked_for_again_goes_on_where_it_was_asked_to(amy, home, kitchen):
    _play(amy, kitchen)
    home.playing()
    assert _play(amy, kitchen, position_s=600).status_code == 200
    home.step()
    assert home.urls()[-1].endswith("/from/10.ts")
    assert len(_streams()) == 1


def test_a_tv_taken_while_the_next_persons_film_starts_is_asked_for_again(
    amy, bob, home, kitchen, monkeypatch
):
    _play(amy, kitchen)
    home.playing()
    _command(amy, kitchen, "stop")
    home.step()
    real = tv_routes._start_cast

    def start(request, body, user, *rest):
        answer = real(request, body, user, *rest)
        if user == "bob":
            assert _play(amy, kitchen).status_code == 200
        return answer

    monkeypatch.setattr(tv_routes, "_start_cast", start)
    refused = _play(bob, kitchen, "102")
    assert (refused.status_code, refused.json()["error"]) == (409, "in_use")
    assert len(_streams()) == 1


def test_a_tv_with_nothing_on_is_taken_without_a_question(amy, bob, home, kitchen):
    _play(amy, kitchen)
    home.playing()
    _command(amy, kitchen, "stop")
    home.step()
    assert _play(bob, kitchen).status_code == 200


def test_it_counts_among_the_three_screens(amy, home, kitchen):
    for key in ("111", "112", "113"):
        amy.post("/api/cast/stream", json={"rating_key": key, "quality": "auto"})
    refused = _play(amy, kitchen)
    assert (refused.status_code, refused.json()["error"]) == (429, "too_many_streams")


def _signed_out(amy, kitchen, conn, admin):
    destroy_session(conn, amy.cookies["session_token"])


def _removed(amy, kitchen, conn, admin):
    delete_user(
        conn, conn.execute("SELECT id FROM admin_users WHERE username='amy'").fetchone()[0], "admin"
    )


def _must_change(amy, kitchen, conn, admin):
    set_must_change_password(conn, "amy", True)


def _forgotten(amy, kitchen, conn, admin):
    assert admin.delete(f"/api/settings/older-tvs/{kitchen}").status_code == 200


def _removed_by_the_tvs_own_address(amy, kitchen, conn, admin):
    assert admin.delete(f"/api/tvs/{kitchen}").status_code == 200


def _turned_off(amy, kitchen, conn, admin):
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'older_tvs_enabled'")
    conn.commit()


def _http_off(amy, kitchen, conn, admin):
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'older_tvs_http'")
    conn.commit()


@pytest.fixture
def admin(whole, conn):
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('admin', 'x', '2026-01-01', 'admin')"
    )
    conn.commit()
    client = TestClient(whole, headers={"User-Agent": "a desk", **ORIGIN})
    session = create_session(conn, "admin", user_agent="a desk", client_ip="testclient")
    client.cookies.set("session_token", session)
    grant_recent_reauth(conn, session, "admin")
    return client


@pytest.mark.parametrize(
    "ending",
    [
        _signed_out,
        _removed,
        _must_change,
        _forgotten,
        _removed_by_the_tvs_own_address,
        _turned_off,
        _http_off,
    ],
)
def test_what_ends_the_play_ends_its_stream_and_ticket_and_stops_the_tv(
    amy,
    admin,
    home,
    kitchen,
    conn,
    set_up,
    manager,  # noqa: F811
    ending,
):
    _play(amy, kitchen)
    home.playing()
    (stream,) = _streams()
    assert set_up.get(_path(home)).status_code == 200
    stops = home.did("Stop")
    ending(amy, kitchen, conn, admin)
    assert home.step() is False
    assert set_up.get(_path(home)).status_code == 404
    assert not _lives(manager, stream)
    assert home.did("Stop") == stops + 1


@pytest.mark.parametrize("forget", [_forgotten, _removed_by_the_tvs_own_address])
def test_an_admin_forgetting_it_ends_the_stream_at_once(
    amy,
    admin,
    home,
    kitchen,
    manager,  # noqa: F811
    forget,
):
    _play(amy, kitchen)
    home.playing()
    (stream,) = _streams()
    forget(amy, kitchen, None, admin)
    assert not _lives(manager, stream)
    told = amy.get(f"/api/tvs/{kitchen}")
    assert (told.status_code, told.json()["error"]) == (410, "unpaired")


def test_a_tv_that_goes_off_ends_the_stream_and_keeps_the_place(
    amy,
    home,
    kitchen,
    conn,
    manager,  # noqa: F811
):
    _play(amy, kitchen)
    home.playing()
    home.status("PLAYING", 90)
    home.step(90)
    (stream,) = _streams()
    home.tv.off = True
    home.step()
    home.step()
    assert home.step(HOLD_SECONDS) is False
    assert not _lives(manager, stream)
    assert _tv_of(amy, kitchen)["last"]["reason"] == "lost"
    assert get_progress(conn, "amy", ["101"])["101"].position_s == 90
    refused = _play(amy, kitchen)
    assert (refused.status_code, refused.json()["error"]) == (502, "unreachable")
    assert _tv_of(amy, kitchen)["on"] is False


def test_another_device_at_the_address_is_handed_nothing(amy, home, kitchen):
    home.tv.pages["/smp_14_"] = http(DESCRIPTION.replace("1234-abcd", "9999"))
    refused = _play(amy, kitchen)
    assert (refused.status_code, refused.json()["error"]) == (409, "another_tv")
    assert home.did("SetAVTransportURI") == 0
    assert _streams() == []


def test_a_viewers_play_looks_nowhere_but_at_the_kept_address(
    amy, home, kitchen, conn, monkeypatch
):
    moved = MovedTv("192.168.1.77")
    monkeypatch.setattr(older_reach, "Wire", moved.wire)
    refused = _play(amy, kitchen)
    assert (refused.status_code, refused.json()["error"]) == (502, "unreachable")
    assert "an admin can Check it in Settings" in refused.json()["message"]
    assert {address for _, (address, _) in moved.sent_udp} == {TV}
    assert repo.every(conn)[0].address == TV


def test_a_person_looks_for_one_tv_at_a_time(amy, bob, home, kitchen):
    with older_reach.looking("person amy"):
        refused = _play(amy, kitchen)
        assert (refused.status_code, refused.json()["error"]) == (409, "looking")
        assert _play(bob, kitchen).status_code == 200


@pytest.mark.parametrize("kept", ["https://a b", "https://*.example"])
def test_an_address_kept_before_that_no_tv_can_use_is_told(amy, home, kitchen, conn, kept):
    conn.execute("UPDATE settings SET value = ? WHERE key = 'tv_address'", (kept,))
    conn.commit()
    refused = _play(amy, kitchen)
    assert (refused.status_code, refused.json()["error"]) == (409, "bad_tv_address")
    assert home.tv.connects == []


def test_a_session_that_ended_while_the_tv_was_asked_plays_and_ends_nothing(
    amy, bob, home, kitchen, conn, monkeypatch
):
    _play(bob, kitchen)
    home.playing()
    (stream,) = _streams()
    real = older_reach.reach

    def slow(*args, **kwargs):
        found = real(*args, **kwargs)
        destroy_session(get_db(), amy.cookies["session_token"])
        return found

    with monkeypatch.context() as slowly:
        slowly.setattr(older_reach, "reach", slow)
        refused = _play(amy, kitchen, "102", confirm=True)
    assert (refused.status_code, refused.json()) == (401, {"detail": "Not authenticated"})
    assert _streams() == [stream]
    assert len(home.drivers) == 1
    assert home.step(driver=0)
    assert _tv_of(bob, kitchen)["play"]["state"] == "playing"


def test_a_session_that_ended_as_the_play_began_leaves_nothing_on(
    amy, home, kitchen, conn, monkeypatch
):
    real = players.Players.begin

    def begin(self, *args, **kwargs):
        destroy_session(get_db(), amy.cookies["session_token"])
        return real(self, *args, **kwargs)

    monkeypatch.setattr(players.Players, "begin", begin)
    assert _play(amy, kitchen).status_code == 401
    assert _streams() == []
    assert home.step() is False
    assert home.urls() == []


def test_a_tv_taken_for_a_play_that_is_about_to_begin_is_asked_for(
    amy, bob, home, kitchen, monkeypatch
):
    real = players.Players.begin
    asked = []

    def begin(self, *args, **kwargs):
        if not asked:
            asked.append(_play(bob, kitchen, "102"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(players.Players, "begin", begin)
    assert _play(amy, kitchen).status_code == 200
    assert (asked[0].status_code, asked[0].json()["error"]) == (409, "in_use")


@pytest.mark.parametrize("broken", ["poll", "forget"])
def test_a_tv_whose_taking_fails_is_stopped_and_free_and_its_stream_given_back(
    amy, bob, home, kitchen, monkeypatch, broken
):
    _play(amy, kitchen)
    home.playing()

    def fails(self, *args, **kwargs):
        raise RuntimeError("broken")

    with monkeypatch.context() as patched:
        patched.setattr(players.Players, broken, fails)
        with pytest.raises(RuntimeError, match="broken"):
            _play(bob, kitchen, "102", confirm=True)
    assert [s.user for s in plex_stream.get_stream_manager()._streams.values()] in ([], ["amy"])
    stops = home.did("Stop")
    home.step(driver=0)
    home.step()
    assert _streams() == []
    assert home.did("Stop") == stops + 1
    assert _play(amy, kitchen).status_code == 200


def test_a_tv_whose_start_came_to_nothing_is_free(amy, bob, home, kitchen, monkeypatch):
    def off(self, *args, **kwargs):
        raise players.TvOff("x")

    with monkeypatch.context() as broken:
        broken.setattr(players.Players, "begin", off)
        assert _play(amy, kitchen).status_code == 409
    assert _play(bob, kitchen).status_code == 200


def test_a_tv_that_moved_is_played_on_where_it_is_now(admin, home, kitchen, conn, monkeypatch):
    moved = MovedTv("192.168.1.77")
    monkeypatch.setattr(older_reach, "Wire", moved.wire)
    home.tv = moved
    home.status("STOPPED")
    assert _play(admin, kitchen).status_code == 200
    home.step()
    assert len(home.urls()) == 1
    row = conn.execute("SELECT * FROM audit_log WHERE action = 'sec:tv.moved'").fetchone()
    assert row["actor"] == "admin"
    assert repo.every(conn)[0].address == "192.168.1.77"


def test_a_viewer_is_told_of_a_host_that_is_not_let_in_without_its_name(
    amy, admin, home, kitchen, monkeypatch
):
    monkeypatch.setenv("MEDIAMAN_ALLOWED_HOSTS", "mediaman.example")
    told = _play(amy, kitchen)
    assert (told.status_code, told.json()["error"]) == (409, "host_not_allowed")
    assert "192.168.1.10" not in told.json()["message"]
    assert "192.168.1.10" in _play(admin, kitchen).json()["message"]
    assert home.tv.connects == []


def test_mediamans_own_address_is_never_played_on(amy, home, kitchen, conn):
    conn.execute("UPDATE settings SET value = ? WHERE key = 'tv_address'", (f"http://{TV}:8282",))
    conn.commit()
    refused = _play(amy, kitchen)
    assert (refused.status_code, refused.json()["error"]) == (422, "own_address")
    assert home.tv.connects == []


@pytest.mark.parametrize(
    ("sql", "status", "error"),
    [
        ("UPDATE settings SET value = 'false' WHERE key = 'older_tvs_enabled'", 404, "not_found"),
        ("UPDATE settings SET value = 'false' WHERE key = 'older_tvs_http'", 409, "http_off"),
        ("DELETE FROM settings WHERE key = 'tv_address'", 409, "no_tv_address"),
    ],
)
def test_the_tv_is_not_asked_unless_the_settings_allow_it(
    amy, home, kitchen, conn, sql, status, error
):
    conn.execute(sql)
    conn.commit()
    refused = _play(amy, kitchen)
    assert (refused.status_code, refused.json()["error"]) == (status, error)
    assert home.tv.connects == []


def test_older_tvs_are_everyones_and_none_is_listed_while_they_are_off(amy, bob, kitchen, conn):
    for who in (amy, bob):
        (tv,) = who.get("/api/tvs").json()["tvs"]
        assert (tv["kind"], tv["name"], tv["on"], tv["busy"]) == (
            "older",
            "Kitchen TV",
            True,
            False,
        )
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'older_tvs_enabled'")
    conn.commit()
    assert amy.get("/api/tvs").json()["tvs"] == []
    assert amy.get(f"/api/tvs/{kitchen}").status_code == 404


def test_a_viewer_neither_renames_nor_removes_it(amy, kitchen):
    assert amy.patch(f"/api/tvs/{kitchen}", json={"name": "x"}).status_code == 404
    assert amy.delete(f"/api/tvs/{kitchen}").status_code == 404
    assert amy.delete(f"/api/settings/older-tvs/{kitchen}").status_code in (401, 403)


def test_settings_tell_whether_it_is_on_and_not_what_plays(amy, admin, home, kitchen):
    _play(amy, kitchen)
    home.playing()
    (tv,) = admin.get("/api/tvs/all").json()["tvs"]
    assert (tv["on"], tv["playing"]) == (True, True)
    assert "play" not in tv


def test_the_tickets_session_is_the_persons_and_not_the_key(amy, home, kitchen, conn):
    _play(amy, kitchen)
    home.playing()
    other = create_session(conn, "amy", user_agent="other", client_ip="testclient")
    destroy_session(conn, other)
    assert home.step()
    assert players.get_players().is_playing(older.get_claims().of(kitchen).key)


def test_a_player_elsewhere_takes_the_title_from_the_tv(amy, home, kitchen):
    _play(amy, kitchen)
    home.playing()
    amy.post("/api/cast/stream", json={"rating_key": "101", "quality": "auto"})
    stops = home.did("Stop")
    home.step()
    assert _tv_of(amy, kitchen)["last"]["reason"] == "moved"
    assert home.did("Stop") == stops + 1


def test_nothing_of_an_earlier_person_of_the_name_is_the_later_ones(amy, home, kitchen, conn):
    _play(amy, kitchen)
    home.playing()
    conn.execute("UPDATE admin_users SET id = id + 50 WHERE username = 'amy'")
    conn.commit()
    (tv,) = amy.get("/api/tvs").json()["tvs"]
    assert tv["play"] is None
    assert home.step() is False
