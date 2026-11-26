"""A viewer manages their own email and sign-ins, and can touch nobody else's."""

from __future__ import annotations

import pytest

from mediaman.db import set_connection
from mediaman.main import create_app
from mediaman.services.rate_limit import limiters
from mediaman.web.auth.password_hash import get_user_email, set_user_email
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.auth.session_store import create_session, list_sessions_for, validate_session
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_LIMITER,
    _SESSIONS_LIST_LIMITER,
    _SESSIONS_REVOKE_LIMITER,
    _USER_MGMT_LIMITER,
)
from tests.helpers.factories import insert_admin_user

_ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def client(conn, viewer_client):
    _USER_MGMT_LIMITER.reset()
    _PASSWORD_CHANGE_LIMITER.reset()
    _SESSIONS_LIST_LIMITER.reset()
    _SESSIONS_REVOKE_LIMITER.reset()
    set_connection(conn)
    insert_admin_user(conn, username="boss")
    set_user_email(conn, "boss", "boss@example.com")
    client = viewer_client(create_app(), conn)
    client.headers.update(_ORIGIN)
    return client


def test_a_viewer_sets_their_own_email_after_confirming_their_password(client, conn):
    grant_recent_reauth(conn, client.cookies["session_token"], "viewer")

    resp = client.patch("/api/users/me/email", json={"email": "vic@example.com"})

    assert resp.status_code == 200
    assert get_user_email(conn, "viewer") == "vic@example.com"
    assert get_user_email(conn, "boss") == "boss@example.com"


def test_a_viewer_s_email_stays_until_they_confirm_their_password(client, conn):
    resp = client.patch("/api/users/me/email", json={"email": "vic@example.com"})

    assert (resp.status_code, resp.json()["reauth_required"]) == (403, True)
    assert get_user_email(conn, "viewer") is None


def test_a_viewer_lists_only_their_own_sign_ins(client, conn):
    create_session(conn, "boss")
    create_session(conn, "viewer")

    resp = client.get("/api/users/sessions")

    assert resp.status_code == 200
    assert len(resp.json()["sessions"]) == 2


def test_a_person_s_list_of_sign_ins_marks_the_one_they_are_on(client, conn):
    mine = list_sessions_for(conn, "viewer")[0]["created_at"]
    create_session(conn, "viewer")
    create_session(conn, "viewer")

    sessions = client.get("/api/users/sessions").json()["sessions"]

    assert [s["created_at"] for s in sessions if s["current"]] == [mine]
    assert len(sessions) == 3


def test_a_viewer_signs_out_their_other_devices_and_nobody_else_s(client, conn):
    theirs = create_session(conn, "boss")
    other = create_session(conn, "viewer")
    mine = client.cookies["session_token"]
    grant_recent_reauth(conn, mine, "viewer")

    resp = client.post("/api/users/sessions/revoke-others")

    assert resp.status_code == 200
    assert validate_session(conn, other) is None
    assert validate_session(conn, mine) is None
    assert len(list_sessions_for(conn, "viewer")) == 1
    assert validate_session(conn, theirs) == "boss"


def test_other_devices_stay_signed_in_until_the_password_is_confirmed(client, conn):
    other = create_session(conn, "viewer")

    for _ in range(_SESSIONS_REVOKE_LIMITER._max_in_window + 1):
        resp = client.post("/api/users/sessions/revoke-others")

    assert (resp.status_code, resp.json()["reauth_required"]) == (403, True)
    assert "session_token" not in resp.cookies
    assert validate_session(conn, other) == "viewer"


def test_a_viewer_signs_out_one_of_their_devices_and_nobody_else_s(client, conn):
    grant_recent_reauth(conn, client.cookies["session_token"], "viewer")
    theirs, other = create_session(conn, "boss"), create_session(conn, "viewer")

    def end(public_id):
        resp = client.delete(f"/api/users/sessions/{public_id}")
        return resp.status_code, resp.text

    mine = next(
        s["public_id"]
        for s in list_sessions_for(conn, "viewer", current_token=other)
        if s["current"]
    )

    assert end(mine) == (200, '{"ok":true}')
    assert validate_session(conn, other) is None
    assert end(list_sessions_for(conn, "boss")[0]["public_id"]) == end("0" * 32)
    assert validate_session(conn, theirs) == "boss"


def test_signing_out_other_devices_is_limited_for_each_person(client, conn):
    other = create_session(conn, "viewer")
    grant_recent_reauth(conn, client.cookies["session_token"], "viewer")
    while _SESSIONS_REVOKE_LIMITER.check("viewer"):
        pass

    resp = client.post("/api/users/sessions/revoke-others")

    assert resp.status_code == 429
    assert validate_session(conn, other) == "viewer"
    assert _SESSIONS_REVOKE_LIMITER.check("boss")


@pytest.mark.parametrize(
    ("limiter", "method", "path", "body"),
    [
        (_USER_MGMT_LIMITER, "PATCH", "/api/users/me/email", {"email": "vic@example.com"}),
        (
            _PASSWORD_CHANGE_LIMITER,
            "POST",
            "/api/users/change-password",
            {"old_password": "x", "new_password": "y"},
        ),
    ],
)
def test_a_limit_met_on_your_own_account_says_so_plainly(
    client, monkeypatch, limiter, method, path, body
):
    monkeypatch.setattr(limiters.time, "monotonic", lambda: 1000.0)
    while limiter.check("viewer"):
        pass

    resp = client.request(method, path, json=body)

    assert resp.status_code == 429
    assert resp.json()["message"] == "Too many changes. Try again in 1 minute."
