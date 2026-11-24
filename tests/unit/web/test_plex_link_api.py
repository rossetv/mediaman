"""Linking, confirming, cancelling and unlinking a Plex account over the API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import encrypt_value
from mediaman.services.plex_link import attempts, repository, sync
from mediaman.services.plex_link.plex_tv import Pin, PlexIdentity, PlexTv
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes import plex_link as routes
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
def app(app_factory, conn, secret_key, plex, clock, reads):
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


def confirm(client, attempt: dict):
    return client.post("/api/account/plex", json={"attempt": attempt["id"]})


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
        ("GET", "/api/users/plex-links"),
        ("DELETE", "/api/users/1/plex-link"),
    ],
)
def test_signed_out_gets_nothing(app, method, path):
    assert TestClient(app).request(method, path).status_code == 401


def test_the_second_to_claim_an_account_is_refused_without_the_firsts_name(
    client_for, plex, clock, conn
):
    amy, bob = client_for("amy"), client_for("bob")
    start(amy)
    start(bob)
    plex.approved = True
    for_amy, for_bob = check(amy), check(bob)
    assert confirm(amy, for_amy).status_code == 200
    resp = confirm(bob, for_bob)
    assert (resp.status_code, resp.json()["error"]) == (409, "claimed")
    assert "amy" not in resp.json()["message"]
    assert repository.get_link(conn, "bob") is None

    start(bob)
    late = check(bob)
    assert (late["state"], late["reason"]) == ("failed", "claimed")
    assert "name" not in late


def test_two_owner_accounts_cannot_both_be_account_one(client_for, plex, conn):
    client_for("bob")
    repository.save_link(conn, "bob", PlexIdentity(9, 1, "owner"), SERVER_ID, ip="")
    plex.resource["owned"] = True
    plex.approved = True
    amy = client_for("amy")
    start(amy)
    assert check(amy)["reason"] == "claimed"


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
        other.post("/api/account/plex/cancel")
    assert repository.list_links(conn) == []
    assert confirm(amy, attempt).status_code == 200


def test_an_attempt_is_not_handed_to_another_user_of_the_same_session():
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


def test_a_sign_in_runs_out(client_for, plex, clock, conn):
    amy = client_for("amy")
    start(amy)
    plex.approved = True
    clock.now += 600
    assert check(amy)["reason"] == "expired"
    assert check(amy) is None
    assert PIN not in plex.paths()
    assert any("link_failed" in line and "expired" in line for line in audit(conn))


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
    start(amy)
    plex.approved = True
    assert amy.post("/api/account/plex/cancel").status_code == 200
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


def test_without_plex_set_up_linking_is_refused(client_for, plex, conn):
    conn.execute("DELETE FROM settings WHERE key = 'plex_token'")
    conn.commit()
    amy = client_for("amy")
    assert amy.get("/api/account/plex").json()["configured"] is False
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
