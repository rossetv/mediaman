"""Linking, confirming, cancelling and unlinking a Plex account over the API."""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import encrypt_value
from mediaman.services.plex_link import attempts, repository, sync
from mediaman.services.plex_link.bounded import Pool, TooBusy, TookTooLong
from mediaman.services.plex_link.plex_tv import Pin, PlexIdentity, PlexTv
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes import plex_link as routes
from mediaman.web.routes.users import router as users_router
from tests.helpers.fake_plex import SERVER, SERVER_ID, TOKEN, FakePlex

TV = "https://plex.tv/api/v2"
PIN = ("GET", f"{TV}/pins/77")


@pytest.fixture
def plex(monkeypatch):
    return FakePlex().install(monkeypatch)


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 1000.0

        def monotonic(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(attempts, "time", clock)
    monkeypatch.setattr(routes, "time", clock)
    return clock


@pytest.fixture
def reads(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(sync, "read_in_background", lambda _db, _key, name: started.append(name))
    return started


@pytest.fixture
def later(monkeypatch):
    """What was put off: (seconds, work), until it is cancelled."""
    waiting: list[tuple[float, object]] = []

    def call_later(seconds, work):
        entry = (seconds, work)
        waiting.append(entry)
        return SimpleNamespace(cancel=lambda: entry in waiting and waiting.remove(entry))

    monkeypatch.setattr(attempts, "_call_later", call_later)
    return waiting


@pytest.fixture
def app(app_factory, conn, secret_key, plex, clock, reads, later):
    token = encrypt_value("admin-token", secret_key, conn=conn, aad=b"plex_token")
    for key, value, encrypted in (("plex_url", SERVER, 0), ("plex_token", token, 1)):
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, '2026')",
            (key, value, encrypted),
        )
    conn.commit()
    routes._attempts.clear()
    yield app_factory(routes.router, conn=conn, state_extras={"db_path": "unused"})
    routes._attempts.clear()


@pytest.fixture
def client_for(app, conn):
    def build(name: str, *, role: str = "viewer", reauth: bool = True) -> TestClient:
        if conn.execute("SELECT 1 FROM admin_users WHERE username=?", (name,)).fetchone() is None:
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, role, created_at)"
                " VALUES (?, 'unused', ?, '2026')",
                (name, role),
            )
            conn.commit()
        token = create_session(conn, name)
        if reauth:
            grant_recent_reauth(conn, token, name)
        client = TestClient(app)
        client.cookies.set("session_token", token)
        return client

    return build


def start(client) -> dict:
    resp = client.post("/api/account/plex")
    assert resp.status_code == 200, resp.text
    return resp.json()["attempt"]


def check(client) -> dict | None:
    resp = client.post("/api/account/plex/check")
    assert resp.status_code == 200, resp.text
    return resp.json()["attempt"]


def confirm(client, attempt: dict, *, move: bool = False):
    return client.post("/api/account/plex", json={"attempt": attempt["id"], "move": move})


def cancel(client, attempt: dict):
    return client.post("/api/account/plex/cancel", json={"attempt": attempt["id"]})


def dismiss(client, moved_at: str):
    return client.delete("/api/account/plex/notice", params={"moved_at": moved_at})


def audit(conn) -> list[str]:
    rows = conn.execute("SELECT action, detail FROM audit_log WHERE action LIKE 'sec:plex%'")
    return [f"{r['action']} {r['detail']}" for r in rows]


def everything(conn) -> str:
    return "\n".join(conn.iterdump())


def test_a_person_links_the_account_they_signed_in_with(client_for, plex, clock, conn, reads):
    amy = client_for("amy")
    attempt = start(amy)
    assert attempt["state"] == "waiting"
    assert attempt["url"].startswith("https://app.plex.tv/auth#?")
    assert repository.get_link(conn, "amy") is None

    plex.approved = True
    attempt = check(amy)
    assert (attempt["state"], attempt["name"]) == ("confirm", "amyplex")
    assert plex.signed_out() == 1
    assert repository.get_link(conn, "amy") is None

    assert confirm(amy, attempt).status_code == 200
    assert repository.get_link(conn, "amy").server_account_id == 4242
    assert reads == ["amy"]
    state = amy.get("/api/account/plex").json()
    assert (state["link"]["name"], state["attempt"]) == ("amyplex", None)


def test_the_token_reaches_no_response_database_or_log(client_for, plex, conn, caplog):
    caplog.set_level("DEBUG")
    amy = client_for("amy")
    said = amy.post("/api/account/plex").text
    plex.approved = True
    said += amy.post("/api/account/plex/check").text + amy.get("/api/account/plex").text
    attempt = amy.get("/api/account/plex").json()["attempt"]
    said += confirm(amy, attempt).text
    for secret in (TOKEN, "admin-token", "server-access-token"):
        assert secret not in said
        assert secret not in everything(conn)
        assert secret not in caplog.text
    assert "77" not in {str(v) for v in attempt.values()}


def test_linking_needs_a_recent_password(client_for, plex):
    resp = client_for("amy", reauth=False).post("/api/account/plex")
    assert (resp.status_code, resp.json()["reauth_required"]) == (403, True)
    assert plex.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/account/plex"),
        ("POST", "/api/account/plex"),
        ("DELETE", "/api/account/plex"),
        ("POST", "/api/account/plex/check"),
        ("POST", "/api/account/plex/cancel"),
        ("DELETE", "/api/account/plex/notice?moved_at=2026"),
        ("GET", "/api/users/plex-links"),
        ("DELETE", "/api/users/1/plex-link"),
    ],
)
def test_signed_out_gets_nothing(app, method, path):
    assert TestClient(app).request(method, path).status_code == 401


def linked(conn, name: str, plex_id: int = 4242, server_id: int = 4242) -> None:
    repository.save_link(conn, name, PlexIdentity(plex_id, server_id, "amyplex"), SERVER_ID, ip="")


def test_whoever_proves_an_account_last_has_it_and_is_told_so_first(client_for, plex, conn):
    bob, amy = client_for("bob"), client_for("amy")
    linked(conn, "bob")
    conn.execute(
        "INSERT INTO plex_plays (username, history_key, media_type, title, viewed_at)"
        " VALUES ('bob', 'h', 'movie', 'T', '2026')"
    )
    for uses_plex in (0, 1):
        conn.execute(
            "INSERT INTO user_picks (username, title, media_type, batch_id, created_at, uses_plex,"
            " tmdb_id) VALUES ('bob', 'T', 'movie', 'b', '2026', ?, ?)",
            (uses_plex, uses_plex),
        )
    conn.commit()
    start(amy)
    plex.approved = True
    attempt = check(amy)
    assert (attempt["state"], attempt["moves"]) == ("confirm", True)
    assert "bob" not in str(attempt)

    untold = confirm(amy, attempt).json()
    assert untold["attempt"]["moves"] is True
    assert repository.get_link(conn, "bob") is not None

    resp = confirm(amy, attempt, move=True)
    assert resp.status_code == 200
    assert "bob" not in resp.text
    assert [link.username for link in repository.list_links(conn)] == ["amy"]
    assert conn.execute("SELECT count(*) FROM plex_plays").fetchone()[0] == 0
    picks = conn.execute("SELECT uses_plex FROM user_picks").fetchall()
    assert [r["uses_plex"] for r in picks] == [0]
    assert bob.get("/api/account/plex").json()["link"] is None
    assert any("moved" in line and "bob" in line for line in audit(conn))
    assert "amyplex" not in "".join(audit(conn))


def test_an_account_claimed_after_the_person_was_asked_is_asked_about_again(client_for, plex, conn):
    amy = client_for("amy")
    client_for("bob")
    start(amy)
    plex.approved = True
    attempt = check(amy)
    assert attempt["moves"] is False
    linked(conn, "bob")
    assert confirm(amy, attempt).json()["attempt"]["moves"] is True
    assert repository.get_link(conn, "amy") is None


def test_a_claim_made_at_the_last_moment_is_refused_without_the_holders_name(
    client_for, plex, conn, monkeypatch
):
    amy = client_for("amy")
    client_for("bob")
    start(amy)
    plex.approved = True
    attempt = check(amy)
    linked(conn, "bob")
    monkeypatch.setattr(repository, "is_claimed_by_other", lambda *_: False)
    resp = confirm(amy, attempt)
    assert (resp.status_code, resp.json()["error"]) == (409, "claimed")
    assert "bob" not in resp.text
    assert [link.username for link in repository.list_links(conn)] == ["bob"]


def test_only_one_person_is_the_servers_owner(client_for, plex, conn):
    client_for("bob")
    linked(conn, "bob", plex_id=9, server_id=1)
    plex.resource["owned"] = True
    plex.approved = True
    amy = client_for("amy")
    start(amy)
    attempt = check(amy)
    assert attempt["moves"] is True
    assert confirm(amy, attempt, move=True).status_code == 200
    assert [link.username for link in repository.list_links(conn)] == ["amy"]


def test_a_move_that_fails_halfway_changes_nothing(client_for, plex, conn, monkeypatch):
    client_for("bob")
    client_for("amy")
    linked(conn, "bob")
    real = repository.security_event_or_raise

    def fail_on_linked(conn, *, event, **rest):
        if event == "plex.linked":
            raise RuntimeError("audit failed")
        real(conn, event=event, **rest)

    monkeypatch.setattr(repository, "security_event_or_raise", fail_on_linked)
    identity = PlexIdentity(4242, 4242, "n")
    with pytest.raises(RuntimeError):
        repository.save_link(conn, "amy", identity, SERVER_ID, ip="", move=True)
    assert [link.username for link in repository.list_links(conn)] == ["bob"]


def test_a_person_already_linked_must_unlink_first(client_for, plex, conn):
    amy = client_for("amy")
    repository.save_link(conn, "amy", PlexIdentity(9, 9, "old"), SERVER_ID, ip="")
    resp = amy.post("/api/account/plex")
    assert (resp.status_code, resp.json()["error"]) == (409, "already_linked")
    assert plex.calls == []


def test_an_attempt_belongs_to_the_session_that_started_it(client_for, plex, conn):
    amy = client_for("amy")
    attempt = start(amy)
    plex.approved = True
    check(amy)
    bob, amys_phone = client_for("bob"), client_for("amy")
    for other in (bob, amys_phone):
        assert check(other) is None
        assert confirm(other, attempt).status_code == 409
        cancel(other, attempt)
    assert repository.list_links(conn) == []
    assert confirm(amy, attempt).status_code == 200


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/users/sessions/revoke-others", None),
        (
            "/api/users/change-password",
            {"old_password": "first pass 12345", "new_password": "Second-Pass-67890"},
        ),
    ],
    ids=["signing out elsewhere", "a new password"],
)
def test_a_sign_in_under_way_outlives_a_new_session_token(
    client_for, app, plex, conn, later, path, body
):
    app.include_router(users_router)
    create_user(conn, "amy", "first pass 12345", enforce_policy=False, role="viewer")
    amy = client_for("amy")
    attempt = start(amy)
    old = amy.cookies["session_token"]
    resp = amy.post(path, json=body)
    assert resp.status_code == 200, resp.text
    new = resp.cookies["session_token"]
    assert new != old
    amy = TestClient(app, cookies={"session_token": new})
    assert check(amy)["id"] == attempt["id"]

    plex.approved = True
    ((_, at_expiry),) = later
    at_expiry()
    assert plex.signed_out() == 1
    assert routes._attempts._attempts == {}


def test_an_attempt_is_not_handed_to_another_user_of_the_same_session(later):
    store = attempts.AttemptStore()
    pin = Pin(id=1, code="c", expires_in_s=60)
    amys = attempts.Attempt("amy", SERVER_ID, PlexTv("c", "p"), pin, expires_at=2.0)
    store.add("session", amys)
    assert store.get("session", "bob") is None
    assert store.get("session", "amy") is amys


def test_nothing_is_saved_before_the_person_has_signed_in_at_plex(client_for, conn):
    amy = client_for("amy")
    attempt = start(amy)
    assert confirm(amy, attempt).json()["error"] == "expired"
    assert repository.get_link(conn, "amy") is None


def test_an_attempt_is_used_once(client_for, plex, conn):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    attempt = check(amy)
    assert confirm(amy, attempt).status_code == 200
    amy.delete("/api/account/plex")
    assert confirm(amy, attempt).status_code == 409
    assert repository.get_link(conn, "amy") is None


def test_a_session_ended_before_confirming_saves_nothing(client_for, plex, conn):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    attempt = check(amy)
    conn.execute("DELETE FROM admin_sessions")
    conn.commit()
    assert confirm(amy, attempt).status_code == 401
    assert repository.list_links(conn) == []


def test_plex_is_asked_once_in_two_seconds(client_for, plex, clock):
    amy = client_for("amy")
    start(amy)
    check(amy)
    check(amy)
    assert plex.paths().count(PIN) == 1
    clock.now += 2
    check(amy)
    assert plex.paths().count(PIN) == 2


def test_a_sign_in_runs_out_and_one_approved_too_late_is_signed_out_unused(
    client_for, plex, clock, conn
):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    clock.now += 600
    failed = check(amy)
    assert (failed["reason"], failed["signed_out"]) == ("expired", True)
    gone = amy.post("/api/account/plex/check").json()
    assert gone["attempt"] is None
    assert gone["message"] == failed["message"]
    assert "Authorized Devices" in gone["message"]
    assert plex.signed_out() == 1
    assert repository.get_link(conn, "amy") is None
    assert any("link_failed" in line and "expired" in line for line in audit(conn))


def test_a_sign_in_nobody_comes_back_to_is_signed_out_when_it_runs_out(
    client_for, plex, clock, later
):
    start(client_for("amy"))
    plex.approved = True
    ((seconds, at_expiry),) = later
    assert seconds == 600
    at_expiry()
    assert plex.signed_out() == 1
    assert PIN in plex.paths()


def test_one_cancelled_and_then_approved_is_signed_out_when_it_runs_out(client_for, plex, later):
    amy = client_for("amy")
    cancel(amy, start(amy))
    assert plex.signed_out() == 0
    plex.approved = True
    later[0][1]()
    assert plex.signed_out() == 1


def test_nothing_is_put_off_for_a_sign_in_already_signed_out(client_for, plex, later):
    amy = client_for("amy")
    start(amy)
    assert len(later) == 1
    plex.approved = True
    check(amy)
    assert later == []


def test_a_last_look_that_fails_is_tried_once_more(client_for, plex, later):
    start(client_for("amy"))
    plex.approved = True
    plex.answers[PIN] = (503, "{}")
    later.pop()[1]()
    ((seconds, again),) = later
    assert 60 <= seconds <= 120
    del plex.answers[PIN]
    later.clear()
    again()
    assert plex.signed_out() == 1
    assert later == []


def test_a_last_look_that_could_not_start_is_tried_once_more(client_for, plex, later, monkeypatch):
    start(client_for("amy"))
    plex.approved = True
    monkeypatch.setattr(attempts, "PLEX_TV", Pool(0))
    later.pop()[1]()
    monkeypatch.setattr(attempts, "PLEX_TV", Pool(1))
    later.pop()[1]()
    assert plex.signed_out() == 1


def test_a_sign_out_that_failed_is_tried_again_when_the_sign_in_runs_out(client_for, plex, later):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    plex.answers["DELETE", f"{TV}/users/signout"] = (500, "{}")
    check(amy)
    del plex.answers["DELETE", f"{TV}/users/signout"]
    later.pop()[1]()
    assert plex.signed_out() == 2
    assert later == []


def test_a_sign_in_that_was_never_approved_needs_one_last_look_only(client_for, later):
    start(client_for("amy"))
    later.pop()[1]()
    assert later == []


def test_cancelling_while_plex_cannot_say_is_not_called_signed_out(client_for, plex):
    amy = client_for("amy")
    attempt = start(amy)
    plex.answers[PIN] = (503, "{}")
    assert cancel(amy, attempt).json()["signed_out"] is False


def test_a_late_cancel_does_not_end_the_next_sign_in(client_for, plex):
    amy = client_for("amy")
    first = start(amy)
    second = start(amy)
    cancel(amy, first)
    plex.approved = True
    assert check(amy)["id"] == second["id"]


def test_cancelling_during_a_poll_waits_for_what_it_gets(client_for, plex):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    (attempt,) = routes._attempts._attempts.values()
    polled, go = threading.Event(), threading.Event()

    def hold(method, where, query):
        if (method, where) == PIN:
            polled.set()
            go.wait(5)

    plex.on_call = hold
    poll = threading.Thread(target=lambda: attempts.advance(attempt, may_poll=lambda: True))
    poll.start()
    assert polled.wait(5)
    answers: list[bool] = []
    discard = threading.Thread(target=lambda: answers.append(attempts.discard(attempt)))
    discard.start()
    go.set()
    discard.join(5)
    poll.join(5)
    assert answers == [True]
    assert plex.paths().count(PIN) == 1


def test_cancelling_says_when_plex_did_not_confirm_the_sign_out(client_for, plex):
    amy = client_for("amy")
    attempt = start(amy)
    plex.approved = True
    plex.answers["DELETE", f"{TV}/users/signout"] = (500, "{}")
    assert cancel(amy, attempt).json()["signed_out"] is False


def test_starting_again_says_when_the_one_before_was_not_signed_out(client_for, plex):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    plex.answers["DELETE", f"{TV}/users/signout"] = (500, "{}")
    assert start(amy)["signed_out"] is False


def test_an_approval_that_takes_too_long_is_given_up_and_still_signed_out(
    client_for, plex, monkeypatch
):
    monkeypatch.setattr(attempts, "_CHAIN_SECONDS", 0.5)
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    over, out = threading.Event(), threading.Event()

    def hang(method, where, query):
        if where.endswith("/user"):
            over.wait(10)
        if method == "DELETE":
            out.set()

    plex.on_call = hang
    failed = check(amy)
    assert (failed["reason"], failed["signed_out"]) == ("plex_unavailable", False)
    over.set()
    assert out.wait(5)


def test_a_full_house_does_not_cost_a_person_one_of_their_starts(client_for):
    amy = client_for("amy")
    for _ in range(30):
        assert routes._SHARED_START_LIMITER.check("everyone")
    for _ in range(6):
        assert amy.post("/api/account/plex").status_code == 429
    routes._SHARED_START_LIMITER.reset()
    for _ in range(5):
        start(amy)


def test_waiting_sign_ins_cannot_use_up_what_starting_one_needs(client_for, plex):
    amy = client_for("amy")
    while routes._SHARED_POLL_LIMITER.check("everyone"):
        pass
    start(amy)
    assert check(amy)["state"] == "waiting"
    assert PIN not in plex.paths()


def test_one_person_over_their_own_limit_spends_nothing_of_everyones(client_for):
    amy, bob = client_for("amy"), client_for("bob")
    for _ in range(40):
        amy.post("/api/account/plex")
    start(bob)
    left = 0
    while routes._SHARED_START_LIMITER.check("everyone"):
        left += 1
    assert left == 30 - 5 - 1


@pytest.mark.parametrize("size", [0, 1], ids=["busy", "slow"])
def test_a_pin_that_takes_too_long_is_plex_being_unreachable(client_for, plex, monkeypatch, size):
    monkeypatch.setattr(attempts, "PLEX_TV", Pool(size))
    monkeypatch.setattr(attempts, "_PIN_SECONDS", 0.05)
    amy, over = client_for("amy"), threading.Event()

    def hang(method, where, query):
        if where.endswith("/pins"):
            over.wait(3)

    plex.on_call = hang
    try:
        assert amy.post("/api/account/plex").status_code == 503
    finally:
        over.set()


def test_a_plex_server_that_hangs_does_not_stop_a_sign_in_under_way(client_for, plex, monkeypatch):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    over = threading.Event()
    try:
        for _ in range(8):
            with contextlib.suppress(TookTooLong, TooBusy):
                sync.PLEX_SERVER.within(0.01, lambda: over.wait(10))
        assert check(amy)["state"] == "confirm"
    finally:
        over.set()


def test_the_person_whose_link_moved_is_told_when_and_not_by_whom_until_they_dismiss_it(
    client_for, plex, conn
):
    bob, amy = client_for("bob"), client_for("amy")
    linked(conn, "bob")
    assert bob.get("/api/account/plex").json()["moved_at"] is None
    start(amy)
    plex.approved = True
    confirm(amy, check(amy), move=True)
    for _ in range(2):
        told = bob.get("/api/account/plex")
        assert "amy" not in told.text.replace("amyplex", "")
    moved_at = told.json()["moved_at"]
    assert moved_at is not None
    assert amy.get("/api/account/plex").json()["moved_at"] is None

    dismiss(amy, moved_at)
    assert bob.get("/api/account/plex").json()["moved_at"] == moved_at
    assert repository.get_link(conn, "amy") is not None
    assert dismiss(bob, moved_at).json()["moved_at"] is None
    assert bob.get("/api/account/plex").json()["moved_at"] is None


def test_dismissing_an_old_notice_leaves_the_one_made_since(client_for, conn):
    bob = client_for("bob")
    conn.execute("INSERT INTO plex_link_moves VALUES ('bob', '2026-03-02T00:00:00+00:00')")
    conn.commit()
    answer = dismiss(bob, "2026-03-01T00:00:00+00:00")
    assert answer.json()["moved_at"] == "2026-03-02T00:00:00+00:00"
    assert bob.get("/api/account/plex").json()["moved_at"] == "2026-03-02T00:00:00+00:00"


def test_dismissing_a_notice_never_unlinks(client_for, conn):
    bob = client_for("bob")
    linked(conn, "bob")
    dismiss(bob, "2026-03-01T00:00:00+00:00")
    assert repository.get_link(conn, "bob") is not None


def test_taking_the_account_back_ends_the_notice_and_tells_the_other(client_for, plex, conn):
    bob, amy = client_for("bob"), client_for("amy")
    linked(conn, "bob")
    plex.approved = True
    for client in (amy, bob):
        start(client)
        assert confirm(client, check(client), move=True).status_code == 200
    bob.delete("/api/account/plex")
    assert bob.get("/api/account/plex").json()["moved_at"] is None
    assert amy.get("/api/account/plex").json()["moved_at"] is not None


@pytest.mark.parametrize(
    "body",
    [
        '<!DOCTYPE m [<!ENTITY a "b">]><MediaContainer machineIdentifier="&a;"/>',
        '<?xml version="1.0" encoding="nope"?><MediaContainer machineIdentifier="a"/>',
    ],
)
def test_a_server_sending_refused_xml_is_said_to_be_unreachable(client_for, plex, body):
    plex.answers["GET", f"{SERVER}/identity"] = (200, body)
    assert client_for("amy").post("/api/account/plex").status_code == 503


def test_a_confirmation_runs_out_too(client_for, plex, clock, conn):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    attempt = check(amy)
    clock.now += 600
    assert confirm(amy, attempt).status_code == 409
    assert repository.get_link(conn, "amy") is None


def test_a_pin_plex_no_longer_knows_has_expired(client_for, plex):
    amy = client_for("amy")
    start(amy)
    plex.answers[PIN] = (404, "{}")
    assert check(amy)["reason"] == "expired"


def test_plex_stumbling_while_waiting_does_not_end_the_sign_in(client_for, plex, clock):
    amy = client_for("amy")
    start(amy)
    plex.answers[PIN] = (503, "{}")
    assert check(amy)["state"] == "waiting"
    del plex.answers[PIN]
    plex.approved = True
    clock.now += 2
    assert check(amy)["state"] == "confirm"


def test_an_account_without_access_to_this_server_is_refused(client_for, plex, conn):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    plex.resource["clientIdentifier"] = "somebody-elses"
    failed = check(amy)
    assert (failed["state"], failed["reason"]) == ("failed", "not_on_server")
    assert amy.get("/api/account/plex").json()["attempt"] is None
    assert plex.signed_out() == 1


def test_a_failed_sign_out_is_said(client_for, plex):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    plex.answers["DELETE", f"{TV}/users/signout"] = (500, "{}")
    assert check(amy)["signed_out"] is False


def test_cancelling_after_approving_signs_the_unused_token_out(client_for, plex, conn):
    amy = client_for("amy")
    attempt = start(amy)
    plex.approved = True
    assert cancel(amy, attempt).status_code == 200
    assert plex.signed_out() == 1
    assert check(amy) is None
    assert repository.get_link(conn, "amy") is None


def test_starting_again_ends_the_sign_in_before_it(client_for, plex, conn):
    amy = client_for("amy")
    first = start(amy)
    second = start(amy)
    plex.approved = True
    check(amy)
    assert confirm(amy, first).status_code == 409
    assert confirm(amy, second).status_code == 200


@pytest.mark.parametrize(
    "down", [("POST", f"{TV}/pins"), ("GET", f"{SERVER}/identity")], ids=["plex.tv", "server"]
)
def test_plex_being_down_is_said_plainly(client_for, plex, down):
    plex.answers[down] = (503, "{}")
    resp = client_for("amy").post("/api/account/plex")
    assert (resp.status_code, resp.json()["error"]) == (503, "plex_unavailable")


@pytest.mark.parametrize(
    "down", [("POST", f"{TV}/pins"), ("GET", f"{SERVER}/identity")], ids=["plex.tv", "server"]
)
def test_plex_being_down_does_not_use_up_a_persons_starts(client_for, plex, down):
    plex.answers[down] = (503, "{}")
    amy = client_for("amy")
    for _ in range(6):
        assert amy.post("/api/account/plex").status_code == 503
    plex.answers.clear()
    for _ in range(5):
        start(amy)
    assert amy.post("/api/account/plex").status_code == 429


def test_a_persons_failed_starts_have_a_cap_and_one_record(client_for, plex, conn):
    plex.answers["POST", f"{TV}/pins"] = (503, "{}")
    amy, bob = client_for("amy"), client_for("bob")
    codes = []
    for _ in range(25):
        codes.append(amy.post("/api/account/plex").status_code)
        routes._SHARED_START_LIMITER.reset()
    assert codes == [503] * 20 + [429] * 5
    asked = len(plex.calls)
    assert amy.post("/api/account/plex").status_code == 429
    assert len(plex.calls) == asked
    assert bob.post("/api/account/plex").status_code == 503
    failed = [line.split(" ip=")[0] for line in audit(conn) if "plex_unavailable" in line]
    assert failed == ["sec:plex.link_failed actor=amy", "sec:plex.link_failed actor=bob"]


def test_starts_that_work_or_are_turned_away_count_for_no_failure(client_for, plex, conn):
    amy = client_for("amy")
    while routes._SHARED_START_LIMITER.check("everyone"):
        pass
    for _ in range(21):
        assert amy.post("/api/account/plex").status_code == 429
    routes._SHARED_START_LIMITER.reset()
    for _ in range(21):
        start(amy)
        routes._START_LIMITER.reset()
    for _ in range(5):
        start(amy)
    for _ in range(21):
        assert amy.post("/api/account/plex").status_code == 429
    routes._START_LIMITER.reset()
    plex.answers["POST", f"{TV}/pins"] = (503, "{}")
    assert amy.post("/api/account/plex").status_code == 503


def test_asking_without_plex_set_up_has_the_same_cap(client_for, conn):
    conn.execute("DELETE FROM settings WHERE key = 'plex_token'")
    conn.commit()
    amy = client_for("amy")
    codes = []
    for _ in range(21):
        codes.append(amy.post("/api/account/plex").status_code)
        routes._SHARED_START_LIMITER.reset()
    assert codes == [409] * 20 + [429]


def test_without_plex_set_up_linking_is_refused(client_for, plex, conn):
    conn.execute("DELETE FROM settings WHERE key = 'plex_token'")
    conn.commit()
    amy = client_for("amy")
    assert amy.get("/api/account/plex").json()["configured"] is False
    for _ in range(6):
        assert amy.post("/api/account/plex").json()["error"] == "plex_not_set_up"
    assert plex.calls == []


def test_starting_is_limited(client_for):
    amy = client_for("amy")
    for _ in range(5):
        start(amy)
    assert amy.post("/api/account/plex").status_code == 429


def test_this_install_keeps_one_identity_at_plex(client_for, plex):
    amy = client_for("amy")
    start(amy)
    start(amy)
    ids = {h["X-Plex-Client-Identifier"] for m, w, h, _ in plex.calls if w.startswith(TV)}
    assert len(ids) == 1


def test_unlinking_is_the_persons_own_and_needs_no_password(client_for, conn):
    client_for("bob")
    for name, number in (("amy", 5), ("bob", 6)):
        client_for(name)
        repository.save_link(conn, name, PlexIdentity(number, number, name), SERVER_ID, ip="")
    assert client_for("amy", reauth=False).delete("/api/account/plex").status_code == 200
    assert [link.username for link in repository.list_links(conn)] == ["bob"]


def test_an_admin_sees_who_is_linked_and_can_unlink_but_not_which_account(client_for, conn):
    client_for("amy")
    repository.save_link(conn, "amy", PlexIdentity(555, 555, "secretname"), SERVER_ID, ip="")
    boss = client_for("boss", role="admin")
    repository.save_link(conn, "boss", PlexIdentity(7, 7, "own"), SERVER_ID, ip="")
    resp = boss.get("/api/users/plex-links")
    (entry,) = resp.json()["links"]
    assert entry["username"] == "amy"
    assert "secretname" not in resp.text
    assert "555" not in resp.text
    assert boss.delete(f"/api/users/{entry['id']}/plex-link").status_code == 200
    assert repository.get_link(conn, "amy") is None
    assert any("actor=boss" in line and "amy" in line for line in audit(conn))
    assert boss.delete(f"/api/users/{entry['id']}/plex-link").status_code == 404


def test_a_viewer_cannot_see_or_end_other_peoples_links(client_for, conn):
    client_for("amy")
    repository.save_link(conn, "amy", PlexIdentity(5, 5, "a"), SERVER_ID, ip="")
    bob = client_for("bob")
    user_id = conn.execute("SELECT id FROM admin_users WHERE username='amy'").fetchone()[0]
    assert bob.get("/api/users/plex-links").status_code == 403
    assert bob.delete(f"/api/users/{user_id}/plex-link").status_code == 403
    assert repository.get_link(conn, "amy") is not None


def test_the_record_of_a_link_says_what_happened_and_no_account(client_for, plex, conn):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    confirm(amy, check(amy))
    amy.delete("/api/account/plex")
    lines = audit(conn)
    assert [line.split()[0] for line in lines] == [
        "sec:plex.link_started",
        "sec:plex.linked",
        "sec:plex.unlinked",
    ]
    assert not any("4242" in line or "amyplex" in line for line in lines)
