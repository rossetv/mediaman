"""Tests for user management API routes (list, create, delete, change-password, sessions)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import authenticate, create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.users import _USER_CREATE_LIMITER, _USER_MGMT_LIMITER
from mediaman.web.routes.users import router as users_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(users_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


def _make_second_user(conn, username: str = "other") -> int:
    create_user(conn, username, "OtherPass!99", enforce_policy=False)
    row = conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()
    return row["id"]


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    for lim in (_USER_MGMT_LIMITER, _USER_CREATE_LIMITER):
        lim._attempts.clear()
        lim._day_counts.clear()
    yield
    for lim in (_USER_MGMT_LIMITER, _USER_CREATE_LIMITER):
        lim._attempts.clear()
        lim._day_counts.clear()


class TestListUsers:
    def test_list_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/users")
        assert resp.status_code == 401

    def test_list_returns_users(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/users")
        assert resp.status_code == 200
        body = resp.json()
        assert "users" in body
        assert "current" in body
        assert body["current"] == "admin"
        assert len(body["users"]) == 1
        assert body["users"][0]["username"] == "admin"


class TestCreateUser:
    def test_create_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/users", json={"username": "newadmin", "password": "ValidPass!99"})
        assert resp.status_code == 401

    def test_create_user_happy_path(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post("/api/users", json={"username": "newadmin", "password": "ValidPass!99"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "username": "newadmin"}
        row = conn.execute("SELECT id FROM admin_users WHERE username='newadmin'").fetchone()
        assert row is not None

    def test_create_user_short_username_returns_400(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post("/api/users", json={"username": "ab", "password": "ValidPass!99"})
        assert resp.status_code == 400
        assert "3 and 64 characters" in resp.json()["error"]

    def test_create_user_long_username_returns_400(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post("/api/users", json={"username": "a" * 65, "password": "ValidPass!99"})
        assert resp.status_code == 400

    def test_create_user_weak_password_returns_400(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post("/api/users", json={"username": "validname", "password": "short"})
        assert resp.status_code == 400
        assert "issues" in resp.json()

    def test_create_user_duplicate_returns_409(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post("/api/users", json={"username": "admin", "password": "ValidPass!99"})
        assert resp.status_code == 409


class TestDeleteUser:
    def test_delete_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        admin_id = conn.execute("SELECT id FROM admin_users WHERE username='admin'").fetchone()[
            "id"
        ]
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(f"/api/users/{admin_id}")
        assert resp.status_code == 401

    def test_delete_self_returns_400(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        admin_id = conn.execute("SELECT id FROM admin_users WHERE username='admin'").fetchone()[
            "id"
        ]
        resp = client.delete(
            f"/api/users/{admin_id}",
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_delete_other_user_happy_path(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)
        resp = client.delete(
            f"/api/users/{other_id}",
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = conn.execute("SELECT id FROM admin_users WHERE id=?", (other_id,)).fetchone()
        assert row is None

    def test_delete_user_requires_password_confirmation(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 403
        assert "Password confirmation required" in resp.json()["error"]

    def test_delete_user_wrong_password_returns_403(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)
        resp = client.delete(
            f"/api/users/{other_id}",
            headers={"X-Confirm-Password": "wrongpassword"},
        )
        assert resp.status_code == 403

    def test_delete_user_rejects_password_in_query_string(self, db_path, secret_key):
        """confirm_password passed as a query param must be rejected with 400.

        Query strings appear in access logs; credentials must not leak there.
        """
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)
        resp = client.delete(
            f"/api/users/{other_id}?confirm_password=password1234",
        )
        assert resp.status_code == 400
        assert "query string" in resp.json()["error"].lower()


class TestChangePassword:
    def test_change_password_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/api/users/change-password", json={"old_password": "x", "new_password": "y"}
        )
        assert resp.status_code == 401

    def test_change_password_happy_path(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert authenticate(conn, "admin", "NewStrongPass!99") is True

    def test_change_password_wrong_old_returns_403(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "wrongpassword", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 403
        assert "Current password is incorrect" in resp.json()["error"]

    def test_change_password_same_as_old_returns_400(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "password1234"},
        )
        assert resp.status_code == 400
        assert "New password must differ" in resp.json()["error"]

    def test_change_password_weak_new_returns_400(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "abc"},
        )
        assert resp.status_code == 400
        assert "issues" in resp.json()


class TestListSessions:
    def test_sessions_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/users/sessions")
        assert resp.status_code == 401

    def test_sessions_returns_list(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/users/sessions")
        assert resp.status_code == 200
        assert "sessions" in resp.json()
        assert len(resp.json()["sessions"]) >= 1


class TestRevokeOtherSessions:
    def test_revoke_others_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/users/sessions/revoke-others")
        assert resp.status_code == 401

    def test_revoke_others_happy_path(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        # Create a second session for the same user
        create_session(conn, "admin")

        resp = client.post("/api/users/sessions/revoke-others")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["revoked"] >= 1
        # A fresh session was issued; exactly one session now remains
        remaining = conn.execute(
            "SELECT COUNT(*) FROM admin_sessions WHERE username='admin'"
        ).fetchone()[0]
        assert remaining == 1
