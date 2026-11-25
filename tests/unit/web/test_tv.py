"""Tests for TVs paired with a code: pairing, the TV's polls, the remote, against a fake Plex."""

from __future__ import annotations

import logging
import re

import defusedxml.ElementTree as ET
import pytest
from fastapi.testclient import TestClient

from mediaman.services.cast import tickets
from mediaman.services.cast.tickets import TICKET_SECONDS, TicketStore
from mediaman.services.media_meta import plex_stream
from mediaman.services.media_meta.plex_stream import (
    IDLE_TIMEOUT_SECONDS,
    TV_IDLE_TIMEOUT_SECONDS,
    StreamGone,
    StreamManager,
)
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexItemNotFound,
    PlexUnavailable,
    PlexWatch,
)
from mediaman.services.tv import codes, players
from mediaman.services.tv.codes import CODE_SECONDS, PairingCodes
from mediaman.services.tv.players import HOLD_SECONDS, Players
from mediaman.web.auth.password_hash import (
    create_user,
    delete_user,
    get_user_role,
    set_must_change_password,
)
from mediaman.web.auth.session_store import create_session, destroy_session
from mediaman.web.repository import tv as repo
from mediaman.web.repository.watch_progress import get_progress, save_progress
from mediaman.web.routes import tv as tv_routes
from mediaman.web.routes.watch import stream_api
from tests.unit.web.test_cast import PLEX_URL, TOKEN, Clock, FakePlex

# Keys from 500 are episodes, from 300 in a library mediaman does not scan.
MOVIE = """<MediaContainer librarySectionID="{section}"><Video type="movie" ratingKey="{key}"
 title="Film" year="2024" duration="6000000">{media}</Video></MediaContainer>"""
EPISODE = """<MediaContainer librarySectionID="1"><Video type="episode" ratingKey="{key}"
 title="The Knife" grandparentTitle="Orchard" grandparentRatingKey="9" parentRatingKey="8"
 parentIndex="1" index="4" duration="2880000">{media}</Video></MediaContainer>"""
MEDIA = """<Media><Part id="7"><Stream id="11" streamType="1"/>
 <Stream id="21" streamType="2" selected="1" displayTitle="English"/>
 <Stream id="31" streamType="3" displayTitle="English"/></Part></Media>"""


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
    monkeypatch.setattr(codes, "_codes", PairingCodes(clock=clock))
    monkeypatch.setattr(players, "_players", Players(clock=clock))

    def metadata(self, key, query=""):
        if key[0] not in "135":
            raise PlexItemNotFound("Item not found in Plex")
        section = "9" if key[0] == "3" else "1"
        text = EPISODE if key[0] == "5" else MOVIE
        root = ET.fromstring(text.format(key=key, section=section, media=MEDIA))
        return root, root[0]

    def next_episode(self, key):
        if int(key) >= 505:
            return None
        return PlexEpisode(str(int(key) + 1), 5, "Windfall", 2880, None, season_index=1)

    monkeypatch.setattr(PlexWatch, "_metadata", metadata)
    monkeypatch.setattr(PlexWatch, "next_episode", next_episode)
    return fake


@pytest.fixture
def manager(plex) -> StreamManager:
    return plex_stream.get_stream_manager()


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
    return app_factory(tv_routes.router, stream_api.router, conn=conn)


@pytest.fixture
def person(app, conn):
    def sign_in(name: str, role: str = "viewer") -> TestClient:
        if get_user_role(conn, name) is None:
            # A real password takes long to hash.
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, created_at, role)"
                " VALUES (?, 'x', '2026-01-01', ?)",
                (name, role),
            )
            conn.commit()
        client = TestClient(app)
        client.cookies.set("session_token", create_session(conn, name))
        return client

    return sign_in


@pytest.fixture
def amy(person):
    return person("amy")


@pytest.fixture
def bob(person):
    return person("bob")


class Tv:
    """A TV's page: what it holds, and the calls it makes."""

    def __init__(self, app) -> None:
        self.client = TestClient(app)
        self.token = "new"
        self.ack = 0

    def post(self, path, body=None, token=None):
        headers = {"X-TV-Token": token or self.token}
        return self.client.post(f"/api/tv/device/{path}", json=body or {}, headers=headers)

    def code(self) -> tuple[str, str]:
        asked = self.post("code").json()
        return asked["code"], asked["token"]

    def pair(self, person, **body) -> int:
        code, token = self.code()
        resp = person.post("/api/tvs/pair", json={"code": code, **body})
        assert resp.status_code == 200, resp.text
        self.token = token
        self.poll()
        return resp.json()["tv"]["id"]

    def poll(self, state=None, position_s=0.0, played_s=0.0, **body) -> dict:
        if state is not None:
            playing = self.poll()["play"]["play_id"]
            body["report"] = {
                "play_id": playing,
                "state": state,
                "position_s": position_s,
                "played_s": played_s,
            }
        resp = self.post("poll", {"ack": self.ack, **body})
        assert resp.status_code == 200, resp.text
        answer = resp.json()
        self.ack = max([self.ack, *(c["id"] for c in answer["commands"])])
        self.kinds = [c["kind"] for c in answer["commands"]]
        return answer

    def ask(self, what):
        return self.post("ask", {"play_id": self.poll()["play"]["play_id"], "what": what})


@pytest.fixture
def tv(app) -> Tv:
    return Tv(app)


@pytest.fixture
def home(tv, amy):
    """Amy's TV: its id."""
    return tv.pair(amy)


def _play(person, tv_id, rating_key="101", **body):
    return person.post(f"/api/tvs/{tv_id}/play", json={"rating_key": rating_key, **body})


def _command(person, tv_id, kind, **body):
    return person.post(f"/api/tvs/{tv_id}/commands", json={"kind": kind, **body})


def _tv_of(person, tv_id) -> dict:
    resp = person.get(f"/api/tvs/{tv_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["tv"]


def _stream_of(tv_token) -> tuple[str, str]:
    _, stream_id, user = players.get_players().stream(tv_routes.hash_token(tv_token))
    return stream_id, user


def _lives(manager, stream_id, user="amy") -> bool:
    try:
        manager.peek(stream_id, user)
    except StreamGone:
        return False
    return True


# ── pairing ──


def test_a_tv_is_paired_by_the_code_it_shows(tv, amy):
    code, token = tv.code()
    assert re.fullmatch("[0-9]{6}", code)
    pending = tv.post("poll", token=token).json()
    assert (pending["state"], pending["expires_in_s"]) == ("pairing", CODE_SECONDS)
    resp = amy.post(
        "/api/tvs/pair", json={"code": f"{code[:3]} {code[3:]}", "name": " Living  room"}
    )
    assert resp.json()["tv"]["name"] == "Living room"
    ready = tv.post("poll", token=token).json()
    assert (ready["state"], ready["name"], ready["owner"]) == ("ready", "Living room", "amy")
    assert [t["name"] for t in amy.get("/api/tvs").json()["tvs"]] == ["Living room"]


def test_a_code_pairs_once_and_not_after_five_minutes(tv, app, amy, bob, clock):
    code, _ = tv.code()
    assert amy.post("/api/tvs/pair", json={"code": code}).status_code == 200
    late, token = Tv(app).code()
    clock.now += CODE_SECONDS
    for used in (code, late, "000", "x" * 6, "٠٠٠٠٠٠"):
        resp = bob.post("/api/tvs/pair", json={"code": used})
        assert (resp.status_code, resp.json()) == (404, {"ok": False, "error": "wrong_code"})
    assert tv.post("poll", token=token).status_code == 404


def test_pairing_needs_a_session(tv):
    code, _ = tv.code()
    assert tv.client.post("/api/tvs/pair", json={"code": code}).status_code == 401


def test_a_person_cannot_go_on_guessing_codes(tv, amy, bob, app):
    for _ in range(10):
        assert amy.post("/api/tvs/pair", json={"code": "x"}).status_code == 404
    code, _ = tv.code()
    refused = amy.post("/api/tvs/pair", json={"code": code})
    assert (refused.status_code, refused.json()["error"]) == (429, "too_many_tries")
    assert int(refused.headers["retry-after"]) > 0
    assert bob.post("/api/tvs/pair", json={"code": code}).status_code == 200


def test_an_address_cannot_go_on_guessing_codes_under_many_names(person):
    for n in range(3):
        guesser = person(f"p{n}")
        for _ in range(10):
            guesser.post("/api/tvs/pair", json={"code": "x"})
    assert person("another").post("/api/tvs/pair", json={"code": "x"}).status_code == 429


def test_nobody_can_go_on_guessing_codes_from_many_addresses(person, monkeypatch):
    addresses = (f"203.0.{n}.1" for n in range(200))
    monkeypatch.setattr(tv_routes, "get_client_ip", lambda request: next(addresses))
    for n in range(10):
        guesser = person(f"p{n}")
        for _ in range(10):
            assert guesser.post("/api/tvs/pair", json={"code": "x"}).status_code == 404
    assert person("another").post("/api/tvs/pair", json={"code": "x"}).status_code == 429


def test_right_codes_count_towards_no_limit(app, amy):
    for _ in range(12):
        tv = Tv(app)
        tv_id = tv.pair(amy)
        assert amy.delete(f"/api/tvs/{tv_id}").status_code == 200


def test_an_address_cannot_go_on_asking_for_codes(tv):
    for _ in range(30):
        assert tv.post("code").status_code == 200
    assert tv.post("code").status_code == 429


def test_another_persons_tv_is_taken_over_only_after_the_question(tv, home, amy, bob, manager):
    _play(amy, home)
    tv.poll("playing", 70)
    stream_id, _ = _stream_of(tv.token)
    old_token = tv.token
    code, token = tv.code()
    asked = bob.post("/api/tvs/pair", json={"code": code})
    assert (asked.status_code, asked.json()) == (
        409,
        {"ok": False, "error": "in_use", "playing": True},
    )
    assert tv.poll()["owner"] == "amy"
    assert _lives(manager, stream_id)

    resp = bob.post("/api/tvs/pair", json={"code": code, "confirm": True})
    assert resp.json()["tv"] == {"id": home, "name": "TV"}
    assert tv.post("poll", token=old_token).status_code == 404
    tv.token = token
    ready = tv.poll()
    assert (ready["owner"], ready["play"]) == ("bob", None)
    assert not _lives(manager, stream_id)
    told = amy.get(f"/api/tvs/{home}")
    assert (told.status_code, told.json()["error"]) == (410, "taken")
    assert told.json()["last"]["position_s"] == 70
    assert _play(amy, home).status_code == 410
    assert amy.get("/api/tvs").json()["tvs"] == []


def test_a_persons_own_tv_is_paired_again_without_a_question_under_its_name(tv, amy):
    tv.pair(amy, name="Bedroom")
    tv_id = tv.pair(amy)
    assert [(t["id"], t["name"]) for t in amy.get("/api/tvs").json()["tvs"]] == [(tv_id, "Bedroom")]


def test_a_start_begun_before_the_tv_changed_hands_never_reaches_it(tv, home, amy, bob, plex):
    code, token = tv.code()

    def take_over():
        plex.on_start = lambda: None
        bob.post("/api/tvs/pair", json={"code": code, "confirm": True})
        tv.post("poll", token=token)

    plex.on_start = take_over
    assert _play(amy, home).status_code == 409
    tv.token = token
    assert tv.poll()["play"] is None


@pytest.mark.parametrize("name", ["", "  ", "x" * 41, "a\x07b"])
def test_a_name_nobody_could_read_is_refused(home, amy, name):
    assert amy.patch(f"/api/tvs/{home}", json={"name": name}).status_code == 422


def test_a_person_has_only_so_many_tvs(app, amy, monkeypatch):
    monkeypatch.setattr(tv_routes.repo, "MAX_TVS_PER_PERSON", 1)
    Tv(app).pair(amy)
    code, _ = Tv(app).code()
    assert amy.post("/api/tvs/pair", json={"code": code}).json()["error"] == "too_many_tvs"


def test_someone_who_must_change_their_password_pairs_nothing(tv, amy, conn):
    set_must_change_password(conn, "amy", True)
    code, token = tv.code()
    assert amy.post("/api/tvs/pair", json={"code": code}).status_code == 404
    assert tv.post("poll", token=token).status_code == 404


def test_a_tv_paired_anew_meanwhile_is_neither_paired_over_nor_removed(tv, home, amy, bob, conn):
    was = repo.find(conn, home)
    tv.pair(amy)
    now = repo.find(conn, home)
    assert not repo.remove(conn, was, actor="root", ip="")
    taken = repo.pair(
        conn,
        username="bob",
        token_hash="other",
        name=None,
        again=was,
        ip="",
        now=tv_routes.now_utc(),
    )
    assert taken is None
    assert repo.find(conn, home) == now


def test_the_audit_log_tells_pairing_taking_over_and_unpairing_apart(tv, home, bob, conn):
    tv.pair(bob, confirm=True)
    bob.delete(f"/api/tvs/{home}")
    rows = conn.execute("SELECT action, actor FROM audit_log ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("sec:tv.paired", "amy"),
        ("sec:tv.taken_over", "bob"),
        ("sec:tv.unpaired", "bob"),
    ]


# ── what ends a pairing ──


def _unpair(amy, tv, conn, tv_id):
    assert amy.delete(f"/api/tvs/{tv_id}").status_code == 200


def _tv_unpairs(amy, tv, conn, tv_id):
    assert tv.post("unpair").status_code == 200


def _user_removed(amy, tv, conn, tv_id):
    create_user(conn, "root", "password1234", enforce_policy=False)
    (user_id,) = conn.execute("SELECT id FROM admin_users WHERE username='amy'").fetchone()
    assert delete_user(conn, user_id, "root")


def _must_change(amy, tv, conn, tv_id):
    set_must_change_password(conn, "amy", True)
    set_must_change_password(conn, "amy", False)


@pytest.mark.parametrize("end", [_unpair, _tv_unpairs, _user_removed, _must_change])
def test_a_tv_that_is_no_longer_paired_is_answered_as_a_stranger(tv, home, amy, conn, manager, end):
    _play(amy, home)
    stream_id, _ = _stream_of(tv.token)
    end(amy, tv, conn, home)
    stranger = tv.post("poll", token="x" * 43)
    assert stranger.status_code == 404
    for path, body in (("poll", {}), ("ask", {"play_id": "x", "what": "stop"})):
        refused = tv.post(path, body)
        assert (refused.status_code, refused.content) == (404, stranger.content)
        assert refused.headers == stranger.headers
    manager.reap()
    assert not _lives(manager, stream_id)


def test_the_phones_sign_out_leaves_the_tv_playing_and_signing_out_everywhere_ends_it(
    tv, home, amy, conn, manager
):
    _play(amy, home)
    stream_id, _ = _stream_of(tv.token)
    destroy_session(conn, amy.cookies["session_token"])
    manager.reap()
    assert tv.poll("playing", 70)["play"]["state"] == "playing"
    assert _lives(manager, stream_id)
    tv_routes.end_plays_of("amy")
    answer = tv.poll()
    assert (answer["state"], answer["play"]) == ("ready", None)
    assert not _lives(manager, stream_id)
    assert get_progress(conn, "amy", ["101"])["101"].position_s == 70


# ── the TV's token and what it opens ──


def test_a_tvs_token_is_no_session(tv, home, app):
    client = TestClient(app)
    client.cookies.set("session_token", tv.token)
    assert client.get("/api/tvs").status_code == 401
    headers = {"X-TV-Token": tv.token, "Authorization": f"Bearer {tv.token}"}
    assert TestClient(app).get(f"/api/tvs/{home}", headers=headers).status_code == 401
    assert TestClient(app).post("/api/watch/stream", headers=headers).status_code == 401


def test_a_session_is_no_tvs_token(home, amy):
    assert amy.post("/api/tv/device/poll", json={}).status_code == 404
    assert amy.post("/api/tv/device/code", json={}).status_code == 404


def test_neither_the_token_nor_the_code_is_in_the_database_or_the_log(tv, amy, conn, caplog):
    caplog.set_level(logging.DEBUG)
    code, token = tv.code()
    amy.post("/api/tvs/pair", json={"code": code})
    tv.token = token
    tv.poll()
    tv_id = amy.get("/api/tvs").json()["tvs"][0]["id"]
    _play(amy, tv_id)
    url = tv.poll()["play"]["url"]
    for kept in ("\n".join(conn.iterdump()), caplog.text):
        assert token not in kept
        assert url.split("/")[3] not in kept
    assert code not in "\n".join(conn.iterdump())


def test_an_address_that_goes_on_failing_is_turned_away_and_a_paired_tv_is_not(tv, home):
    for _ in range(60):
        assert tv.post("poll", token="x" * 43).status_code == 404
    assert tv.post("poll", token="x" * 43).status_code == 429
    assert tv.post("poll").status_code == 200


def test_a_tv_that_polls_without_pause_is_held_back(tv, home):
    _, waiting = tv.code()
    for _ in range(149):
        assert tv.post("poll").status_code == 200
        assert tv.post("poll", token=waiting).status_code == 200
    assert tv.post("poll", token=waiting).status_code == 200
    assert tv.post("poll").status_code == 429
    assert tv.post("poll", token=waiting).status_code == 429


def test_a_body_larger_than_any_there_is_is_refused(tv, home):
    padded = '{"ack": 0' + " " * 17000 + "}"
    headers = {"X-TV-Token": tv.token}
    resp = tv.client.post("/api/tv/device/poll", content=padded, headers=headers)
    assert resp.status_code == 422


# ── whose a TV is ──


def test_another_persons_tv_is_as_one_that_never_was(home, bob):
    for tv_id in (home, 999):
        answers = [
            bob.get(f"/api/tvs/{tv_id}"),
            bob.patch(f"/api/tvs/{tv_id}", json={"name": "Mine"}),
            bob.delete(f"/api/tvs/{tv_id}"),
            _play(bob, tv_id),
            _command(bob, tv_id, "stop"),
        ]
        assert [(a.status_code, a.json()) for a in answers] == [
            (404, {"ok": False, "error": "not_found"})
        ] * len(answers)


def test_an_admin_sees_renames_and_removes_every_tv_but_plays_on_none(
    tv, home, amy, bob, person, conn
):
    root = person("root", role="admin")
    _play(amy, home)
    (listed,) = root.get("/api/tvs/all").json()["tvs"]
    assert (listed["owner"], listed["playing"], listed["on"]) == ("amy", True, True)
    assert "Film" not in root.get("/api/tvs/all").text
    assert bob.get("/api/tvs/all").status_code == 403
    assert root.get(f"/api/tvs/{home}").status_code == 404
    assert _play(root, home).status_code == 404
    assert _command(root, home, "stop").status_code == 404
    assert root.patch(f"/api/tvs/{home}", json={"name": "Den"}).status_code == 200
    assert amy.get("/api/tvs").json()["tvs"][0]["name"] == "Den"
    assert root.delete(f"/api/tvs/{home}").status_code == 200
    assert tv.post("poll").status_code == 404
    assert amy.get(f"/api/tvs/{home}").json()["error"] == "unpaired"


def test_an_id_no_tv_can_have_is_refused(home, amy):
    for tv_id in ("0", "-1", "9" * 30, "x"):
        assert amy.get(f"/api/tvs/{tv_id}").status_code == 422


# ── playing ──


def test_the_tv_is_given_the_owners_stream_and_the_remote_is_not_given_the_ticket(
    tv, home, amy, manager
):
    resp = _play(amy, home, "501", position_s=120)
    assert resp.status_code == 200, resp.text
    play = tv.poll()["play"]
    assert play["url"].startswith("/api/cast/")
    assert (play["title"], play["season"], play["episode"], play["episode_title"]) == (
        "Orchard",
        1,
        4,
        "The Knife",
    )
    assert (play["start_s"], play["duration_s"]) == (120, 2880)
    assert play["next"] == {"rating_key": "502", "season": 1, "episode": 5, "title": "Windfall"}
    stream_id, user = _stream_of(tv.token)
    assert user == "amy"
    assert _lives(manager, stream_id)
    ticket = play["url"].split("/")[3]
    for text in (resp.text, amy.get("/api/tvs").text, amy.get(f"/api/tvs/{home}").text):
        assert ticket not in text
    shown = _tv_of(amy, home)
    assert (shown["play"]["subtitle"], shown["play"]["subtitle_tracks"][0]["id"]) == (None, "31")


def test_a_title_goes_on_from_the_place_the_person_left_it(tv, home, amy, conn):
    save_progress(
        conn,
        username="amy",
        rating_key="101",
        media_item_id="101",
        show_rating_key=None,
        position_s=600,
        duration_s=6000,
        played_s=0,
        now=tv_routes.now_utc(),
    )
    _play(amy, home)
    assert tv.poll()["play"]["start_s"] == 600


@pytest.mark.parametrize("key", ["301", "201"])
def test_only_what_the_person_could_watch_plays(tv, home, amy, key):
    _play(amy, home)
    tv.poll("playing", 70)
    assert _play(amy, home, key).status_code == 404
    assert tv.poll()["play"]["state"] == "playing", "a start that was refused stops nothing"


def test_nothing_starts_on_a_tv_that_does_not_answer(tv, home, amy, clock, plex):
    clock.now += 11
    resp = _play(amy, home)
    assert (resp.status_code, resp.json()["error"]) == (409, "tv_off")
    assert plex.calls == []


def test_a_tv_counts_among_the_three_screens(tv, home, amy):
    for key in ("102", "103", "104"):
        amy.post("/api/watch/stream", json={"rating_key": key, "quality": "best"})
    resp = _play(amy, home)
    assert (resp.status_code, resp.json()["error"]) == (429, "too_many_streams")
    play = tv.poll()["play"]
    assert (play["state"], play["error"]) == ("failed", "too_many_streams")


def test_what_plays_on_the_tv_makes_room_for_what_plays_next(tv, home, amy, manager):
    for key in ("102", "103"):
        amy.post("/api/watch/stream", json={"rating_key": key, "quality": "best"})
    _play(amy, home)
    first, _ = _stream_of(tv.token)
    assert _play(amy, home, "105").status_code == 200
    assert not _lives(manager, first)


def test_a_start_plex_fails_is_told_to_both(tv, home, amy, plex):
    plex.reads_fail = True
    resp = _play(amy, home)
    assert (resp.status_code, resp.json()["error"]) == (502, "unreachable")
    assert tv.poll()["play"]["error"] == "unreachable"
    plex.reads_fail = False
    assert tv.ask("again").status_code == 200
    assert tv.poll()["play"]["state"] == "loading"


def test_the_place_is_saved_as_the_persons_own(tv, home, amy, conn, clock):
    _play(amy, home)
    clock.now += 11
    tv.poll()
    tv.poll("playing", 400, 10)
    assert get_progress(conn, "amy", ["101"])["101"].position_s == 400
    tv.poll("paused", 405, 15)
    assert get_progress(conn, "amy", ["101"])["101"].position_s == 405
    tv.poll("playing", 410, 20)
    _command(amy, home, "stop")
    assert get_progress(conn, "amy", ["101"])["101"].position_s == 410


def test_a_tv_cannot_claim_to_have_played_for_longer_than_it_has(tv, home, amy, conn, clock):
    _play(amy, home)
    clock.now += 11
    tv.poll("paused", 400, 3000)
    row = conn.execute("SELECT credited_at FROM watch_progress").fetchone()
    assert row["credited_at"] is None


def test_a_paused_tv_keeps_its_stream_while_it_answers(tv, home, amy, manager, clock):
    _play(amy, home)
    tv.poll("paused", 400)
    stream_id, _ = _stream_of(tv.token)
    for _ in range(int(TV_IDLE_TIMEOUT_SECONDS / 100) + 1):
        clock.now += 100
        tv_routes._POLL_LIMITER.reset()
        tv.poll("paused", 400)
        manager.reap()
    assert _lives(manager, stream_id)


def test_a_tv_away_too_long_loses_its_stream_and_keeps_the_place(
    tv, home, amy, manager, clock, conn
):
    _play(amy, home)
    tv.poll("playing", 70)
    stream_id, _ = _stream_of(tv.token)
    clock.now += HOLD_SECONDS
    manager.reap()
    assert _lives(manager, stream_id)
    clock.now += 1
    manager.reap()
    assert not _lives(manager, stream_id)
    assert get_progress(conn, "amy", ["101"])["101"].position_s == 70
    assert _tv_of(amy, home)["last"]["reason"] == "lost"


def test_a_stream_that_ended_under_the_tv_is_asked_for_again_from_its_place(tv, home, amy, manager):
    _play(amy, home)
    tv.poll("playing", 70)
    manager.end(*_stream_of(tv.token))
    tv.poll()
    assert tv.poll()["play"]["error"] == "gone"
    assert tv.ask("again").status_code == 200
    play = tv.poll()["play"]
    assert (play["state"], play["start_s"]) == ("loading", 70)


def test_the_title_played_elsewhere_leaves_the_tv_and_is_not_taken_back(tv, home, amy, clock):
    _play(amy, home)
    play_id = tv.poll()["play"]["play_id"]
    clock.now += 5
    body = {"rating_key": "101", "quality": "best", "page": "phone-page"}
    assert amy.post("/api/watch/stream", json=body).status_code == 200
    assert tv.post("ask", {"play_id": play_id, "what": "again"}).status_code == 410
    assert tv.poll()["play"] is None
    assert _tv_of(amy, home)["last"]["reason"] == "moved"


def test_the_tvs_poll_learns_that_the_title_plays_elsewhere(tv, home, amy):
    _play(amy, home)
    amy.post("/api/watch/stream", json={"rating_key": "101", "quality": "best"})
    assert tv.poll()["play"] is None
    assert _tv_of(amy, home)["last"]["reason"] == "moved"


def test_a_place_past_the_end_is_the_end(tv, home, amy):
    _play(amy, home, "501", position_s=9000)
    assert tv.poll()["play"]["start_s"] == 2880


def test_a_film_that_is_over_plays_again_from_the_start_and_restarts_for_nothing_else(
    tv, home, amy
):
    _play(amy, home)
    tv.poll("ended", 6000)
    assert _command(amy, home, "quality", quality="low").json()["error"] == "not_playing"
    assert _command(amy, home, "again").status_code == 200
    play = tv.poll()["play"]
    assert (play["state"], play["start_s"]) == ("loading", 0)


# ── the remote ──


def test_the_remotes_commands_reach_the_tv_in_order_and_are_acknowledged(tv, home, amy):
    _play(amy, home)
    tv.poll("playing", 70)
    tv.poll()
    for kind, body in (
        ("pause", {}),
        ("seek", {"position_s": 300}),
        ("volume", {"level": 0.4}),
        ("mute", {"muted": True}),
    ):
        last = _command(amy, home, kind, **body)
        assert last.status_code == 200, last.text
    commands = tv.post("poll", {"ack": tv.ack}).json()["commands"]
    assert [(c["kind"], c.get("position_s"), c.get("level"), c.get("muted")) for c in commands] == [
        ("pause", None, None, None),
        ("seek", 300, None, None),
        ("volume", None, 0.4, None),
        ("mute", None, None, True),
    ]
    tv.poll()
    tv.poll()
    assert _tv_of(amy, home)["acked"] == last.json()["id"]


@pytest.mark.parametrize("kind", ["seek", "volume", "mute", "quality", "subtitles"])
def test_a_command_without_what_it_is_about_is_refused(tv, home, amy, kind):
    _play(amy, home)
    assert _command(amy, home, kind).status_code == 422


@pytest.mark.parametrize("position", ["NaN", "Infinity", "-1"])
def test_a_place_that_is_no_time_is_refused(tv, home, amy, position):
    _play(amy, home)
    raw = f'{{"kind": "seek", "position_s": {position}}}'
    resp = amy.post(
        f"/api/tvs/{home}/commands", content=raw, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 422
    raw = f'{{"kind": "volume", "level": {position}}}'
    assert (
        amy.post(
            f"/api/tvs/{home}/commands", content=raw, headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )
    raw = f'{{"report": {{"play_id": "x", "state": "playing", "position_s": {position}}}}}'
    resp = tv.client.post(
        "/api/tv/device/poll",
        content=raw,
        headers={"Content-Type": "application/json", "X-TV-Token": tv.token},
    )
    assert resp.status_code == 422


def test_subtitles_and_quality_restart_the_picture_where_it_is(tv, home, amy, plex):
    _play(amy, home)
    first = tv.poll("playing", 70)["play"]
    assert _command(amy, home, "subtitles", subtitle="31").status_code == 200
    play = tv.poll()["play"]
    assert play["play_id"] != first["play_id"]
    assert (play["start_s"], tv.kinds) == (70, ["load"])
    assert plex.decision()["subtitleStreamID"] == "31"
    assert _command(amy, home, "quality", quality="low").status_code == 200
    assert plex.decision()["subtitleStreamID"] == "31", "the subtitles stay"
    assert _tv_of(amy, home)["play"]["quality"] == "low"


def test_the_next_episode_is_not_asked_for_in_the_lasts_subtitle_track(tv, home, amy, plex):
    _play(amy, home, "501", subtitle="31")
    assert _command(amy, home, "next").status_code == 200
    assert "subtitleStreamID" not in plex.decision()
    _command(amy, home, "subtitles", subtitle="off")
    _command(amy, home, "next")
    assert plex.decision()["subtitles"] == "none"


def test_of_two_starts_the_later_plays(tv, home, amy, plex, manager):
    def press_another():
        plex.on_start = lambda: None
        assert _play(amy, home, "102").status_code == 200

    plex.on_start = press_another
    assert _play(amy, home, "101").json()["error"] == "superseded"
    assert _tv_of(amy, home)["play"]["rating_key"] == "102"
    stream_id, _ = _stream_of(tv.token)
    assert list(manager._streams) == [stream_id]


def test_a_stream_ends_when_its_ticket_would(tv, home, amy, manager, clock):
    _play(amy, home)
    stream_id, _ = _stream_of(tv.token)
    until = clock.now + TICKET_SECONDS
    while clock.now < until - 100:
        clock.now += 100
        tv_routes._POLL_LIMITER.reset()
        tv.poll()
    manager.reap()
    assert _lives(manager, stream_id)
    clock.now += 100
    manager.reap()
    assert not _lives(manager, stream_id)


def test_the_next_episode_begins_by_itself_once_the_countdown_is_over(tv, home, amy, clock):
    _play(amy, home, "501")
    assert tv.poll("ended", 2880)["play"]["state"] == "upnext"
    assert tv.ask("auto").status_code == 409
    clock.now += 10
    assert tv.ask("auto").status_code == 200
    play = tv.poll()["play"]
    assert (play["state"], play["next"]["rating_key"]) == ("loading", "503")
    assert tv.ask("auto").status_code == 409


def test_the_remote_goes_on_to_the_next_episode_and_after_the_last_is_told(tv, home, amy):
    _play(amy, home, "504")
    assert _command(amy, home, "next").status_code == 200
    assert _tv_of(amy, home)["play"]["rating_key"] == "505"
    assert _command(amy, home, "next").json()["error"] == "no_next"
    assert tv.poll("ended", 2880)["play"]["state"] == "ended"


def test_what_follows_is_asked_for_again_where_plex_could_not_say(
    tv, home, amy, monkeypatch, clock
):
    answers = PlexWatch.next_episode

    def fails(self, key):
        raise PlexUnavailable("no")

    monkeypatch.setattr(PlexWatch, "next_episode", fails)
    _play(amy, home, "501")
    play = tv.poll("ended", 2880)["play"]
    assert play["state"] == "upnext"
    assert play["next"] == {"season": None, "episode": None, "title": None}
    clock.now += 10
    assert tv.ask("auto").status_code == 502
    monkeypatch.setattr(PlexWatch, "next_episode", answers)
    assert tv.ask("auto").status_code == 200
    assert _tv_of(amy, home)["play"]["rating_key"] == "502"


def test_either_screen_answers_still_watching(tv, home, amy, clock):
    _play(amy, home, "501")
    for _ in range(3):
        assert tv.poll("ended", 2880)["play"]["state"] == "upnext"
        clock.now += 10
        assert tv.ask("auto").status_code == 200
    assert tv.poll("ended", 2880)["play"]["state"] == "still"
    assert tv.ask("auto").status_code == 409
    assert _command(amy, home, "next").status_code == 200
    assert _tv_of(amy, home)["play"]["rating_key"] == "505"


def test_a_command_about_what_no_longer_plays_is_refused(tv, home, amy):
    _play(amy, home)
    old = tv.poll()["play"]["play_id"]
    _play(amy, home, "102")
    for kind in ("pause", "stop", "again"):
        assert _command(amy, home, kind, play_id=old).json()["error"] == "stale"
    assert tv.post("ask", {"play_id": old, "what": "stop"}).status_code == 409
    assert tv.poll()["play"] is not None


def test_a_stop_gives_the_place_back_and_works_with_the_tv_away(tv, home, amy, manager, clock):
    _play(amy, home)
    tv.poll("playing", 70)
    stream_id, _ = _stream_of(tv.token)
    clock.now += 60
    assert _command(amy, home, "pause").json()["error"] == "tv_off"
    stopped = _command(amy, home, "stop").json()["tv"]
    assert (stopped["play"], stopped["last"]["position_s"]) == (None, 70)
    assert not _lives(manager, stream_id)


def test_a_stream_nobody_reads_at_all_still_ends(tv, home, amy, manager, clock):
    """Once the TV's page is gone from the list, nothing keeps it alive."""
    _play(amy, home)
    stream_id, _ = _stream_of(tv.token)
    clock.now += IDLE_TIMEOUT_SECONDS + 1
    manager.reap()
    assert not _lives(manager, stream_id)


def test_the_art_of_what_plays_is_there_while_it_plays(tv, home, amy, monkeypatch):
    asked = []

    def thumb(request, rating_key, username):
        asked.append((rating_key, username))
        return tv_routes.respond_ok()

    monkeypatch.setattr(tv_routes, "api_watch_thumb", thumb)
    _play(amy, home)
    art = tv.poll()["play"]["art_url"]
    assert tv.client.get(art).status_code == 200
    assert asked == [("101", "amy")]
    _command(amy, home, "stop")
    assert tv.client.get(art).status_code == 404
