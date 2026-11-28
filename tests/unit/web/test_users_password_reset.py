"""An admin gives another user a temporary password: only behind a password re-check, never for
themselves, and the user is signed out everywhere and must choose their own at the next sign-in.
"""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.web.auth.password_hash import (
    create_user,
    user_must_change_password,
    verify_password,
)
from mediaman.web.auth.password_policy import password_issues
from mediaman.web.auth.reauth import grant_recent_reauth, has_recent_reauth
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.users import _UNTICKETED_LIMITER, _USER_MGMT_LIMITER
from mediaman.web.routes.users import router as users_router


@pytest.fixture(autouse=True)
def _clear_limiters():
    _USER_MGMT_LIMITER.reset()
    _UNTICKETED_LIMITER.reset()
    yield
    _USER_MGMT_LIMITER.reset()
    _UNTICKETED_LIMITER.reset()


def _user(conn, username: str, role: str = "viewer") -> int:
    create_user(conn, username, "OldPass!word99", role=role, enforce_policy=False)
    return conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()["id"]


def _sessions(conn, username: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM admin_sessions WHERE username=?", (username,)
    ).fetchone()[0]


def test_reset_gives_a_working_temporary_password_and_signs_the_user_out(
    app_factory, authed_client, conn
):
    client = authed_client(app_factory(users_router, conn=conn), conn, with_reauth=True)
    bob = _user(conn, "bob")
    bob_token = create_session(conn, "bob")
    create_session(conn, "bob")
    grant_recent_reauth(conn, bob_token, "bob")

    resp = client.post(f"/api/users/{bob}/reset-password")

    assert resp.status_code == 200
    temp = resp.json()["password"]
    assert password_issues(temp, username="bob") == []
    assert verify_password(conn, "bob", temp)
    assert not verify_password(conn, "bob", "OldPass!word99")
    assert user_must_change_password(conn, "bob")
    assert _sessions(conn, "bob") == 0
    assert not has_recent_reauth(conn, bob_token, "bob")
    # the admin's own session is untouched
    assert _sessions(conn, "admin") == 1
    row = conn.execute(
        "SELECT actor, detail FROM audit_log WHERE action='sec:user.password_reset'"
    ).fetchone()
    assert row is not None and "bob" in row["detail"]
    assert temp not in row["detail"]


def test_another_admins_password_can_be_reset_too(app_factory, authed_client, conn):
    client = authed_client(app_factory(users_router, conn=conn), conn, with_reauth=True)
    eve = _user(conn, "eve", role="admin")
    create_session(conn, "eve")

    resp = client.post(f"/api/users/{eve}/reset-password")

    assert resp.status_code == 200
    assert verify_password(conn, "eve", resp.json()["password"])
    assert user_must_change_password(conn, "eve")
    assert _sessions(conn, "eve") == 0


def test_a_failed_audit_row_leaves_the_password_and_sessions_as_they_were(
    app_factory, authed_client, conn, monkeypatch
):
    import mediaman.core.audit as audit_module

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated audit failure")

    client = authed_client(app_factory(users_router, conn=conn), conn, with_reauth=True)
    bob = _user(conn, "bob")
    create_session(conn, "bob")
    monkeypatch.setattr(audit_module, "security_event_or_raise", boom)

    resp = client.post(f"/api/users/{bob}/reset-password")

    assert resp.status_code == 500
    assert verify_password(conn, "bob", "OldPass!word99")
    assert not user_must_change_password(conn, "bob")
    assert _sessions(conn, "bob") == 1


def test_reset_needs_a_recent_password_re_check(app_factory, authed_client, conn):
    client = authed_client(app_factory(users_router, conn=conn), conn)
    bob = _user(conn, "bob")
    create_session(conn, "bob")

    resp = client.post(f"/api/users/{bob}/reset-password")

    assert resp.status_code == 403
    assert resp.json()["reauth_required"] is True
    assert verify_password(conn, "bob", "OldPass!word99")
    assert not user_must_change_password(conn, "bob")
    assert _sessions(conn, "bob") == 1


def test_an_admin_cannot_reset_their_own_password(app_factory, authed_client, conn):
    client = authed_client(app_factory(users_router, conn=conn), conn, with_reauth=True)
    me = conn.execute("SELECT id FROM admin_users WHERE username='admin'").fetchone()["id"]

    resp = client.post(f"/api/users/{me}/reset-password")

    assert resp.status_code == 400
    assert verify_password(conn, "admin", "password1234")
    assert not user_must_change_password(conn, "admin")


def test_a_viewer_signed_in_cannot_reset_anyones_password(app_factory, authed_client, conn):
    client = authed_client(app_factory(users_router, conn=conn), conn, username="vic")
    conn.execute("UPDATE admin_users SET role='viewer' WHERE username='vic'")
    conn.commit()
    grant_recent_reauth(conn, client.cookies.get("session_token"), "vic")
    bob = _user(conn, "bob", role="admin")

    resp = client.post(f"/api/users/{bob}/reset-password")

    assert resp.status_code == 403
    assert verify_password(conn, "bob", "OldPass!word99")


def test_an_unknown_user_is_not_found(app_factory, authed_client, conn):
    client = authed_client(app_factory(users_router, conn=conn), conn, with_reauth=True)
    assert client.post("/api/users/99999/reset-password").status_code == 404


def test_resets_are_rate_limited_like_other_user_actions(app_factory, authed_client, conn):
    client = authed_client(app_factory(users_router, conn=conn), conn, with_reauth=True)
    bob = _user(conn, "bob")
    codes = [client.post(f"/api/users/{bob}/reset-password").status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
