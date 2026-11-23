"""Role management over the users API: create, list, change, and the last-admin refusals."""

from __future__ import annotations

import pytest

from mediaman.web.auth.password_hash import get_user_role, user_must_change_password
from mediaman.web.routes.users import (
    _REAUTH_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
)
from mediaman.web.routes.users import router as users_router
from tests.helpers.factories import insert_admin_user

_STRONG = "Correct-Horse-Battery-9!"


@pytest.fixture(autouse=True)
def _reset_limiters():
    for lim in (_USER_MGMT_LIMITER, _USER_CREATE_LIMITER, _REAUTH_LIMITER):
        lim.reset()
    yield
    for lim in (_USER_MGMT_LIMITER, _USER_CREATE_LIMITER, _REAUTH_LIMITER):
        lim.reset()


@pytest.fixture
def client(app_factory, authed_client, conn):
    return authed_client(app_factory(users_router, conn=conn), conn, with_reauth=True)


def _id(conn, username):
    return conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()["id"]


def test_a_user_created_as_viewer_is_a_viewer_who_must_change_their_password(client, conn):
    resp = client.post(
        "/api/users", json={"username": "vic", "password": _STRONG, "role": "viewer"}
    )

    assert resp.status_code == 200
    assert get_user_role(conn, "vic") == "viewer"
    assert user_must_change_password(conn, "vic") is True


def test_the_api_default_role_is_admin_and_still_forces_a_password_change(client, conn):
    resp = client.post("/api/users", json={"username": "newadmin", "password": _STRONG})

    assert resp.status_code == 200
    assert get_user_role(conn, "newadmin") == "admin"
    assert user_must_change_password(conn, "newadmin") is True


def test_an_unknown_role_is_rejected_and_creates_nobody(client, conn):
    resp = client.post("/api/users", json={"username": "vic", "password": _STRONG, "role": "root"})

    assert resp.status_code == 422
    assert get_user_role(conn, "vic") is None


def test_the_listing_carries_each_users_role(client, conn):
    insert_admin_user(conn, username="vic", role="viewer")

    users = {u["username"]: u["role"] for u in client.get("/api/users").json()["users"]}

    assert users == {"admin": "admin", "vic": "viewer"}


def test_an_admin_can_change_another_users_role(client, conn):
    insert_admin_user(conn, username="vic", role="viewer")

    resp = client.patch(f"/api/users/{_id(conn, 'vic')}/role", json={"role": "admin"})

    assert resp.status_code == 200
    assert get_user_role(conn, "vic") == "admin"


def test_changing_a_role_needs_a_recent_reauth(app_factory, authed_client, conn):
    client = authed_client(app_factory(users_router, conn=conn), conn)
    insert_admin_user(conn, username="vic", role="viewer")

    resp = client.patch(f"/api/users/{_id(conn, 'vic')}/role", json={"role": "admin"})

    assert resp.status_code == 403
    assert resp.json()["reauth_required"] is True
    assert get_user_role(conn, "vic") == "viewer"


def test_you_cannot_change_your_own_role(client, conn):
    insert_admin_user(conn, username="other")

    resp = client.patch(f"/api/users/{_id(conn, 'admin')}/role", json={"role": "viewer"})

    assert resp.status_code == 400
    assert get_user_role(conn, "admin") == "admin"


def test_a_session_demoted_since_it_signed_in_is_refused(client, conn):
    insert_admin_user(conn, username="other")
    conn.execute("UPDATE admin_users SET role='viewer' WHERE username='admin'")
    conn.commit()

    resp = client.patch(f"/api/users/{_id(conn, 'other')}/role", json={"role": "viewer"})

    assert resp.status_code == 403
    assert get_user_role(conn, "other") == "admin"


def test_the_only_admin_deleting_themselves_is_told_they_are_the_last_admin(client, conn):
    insert_admin_user(conn, username="vic", role="viewer")

    resp = client.delete(f"/api/users/{_id(conn, 'admin')}")

    assert resp.status_code == 400
    assert resp.json()["error"] == "last_admin"
    assert get_user_role(conn, "admin") == "admin"
