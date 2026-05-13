"""Tests for user management API routes (list, create, delete, change-password, sessions)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.password_hash import authenticate, create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    _REAUTH_LIMITER,
    _SESSIONS_LIST_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
)
from mediaman.web.routes.users import router as users_router


def _app(app_factory, conn):
    return app_factory(users_router, conn=conn)


def _client(app_factory, authed_client, conn, *, with_reauth: bool = True):
    """Build the user-routes app + an authed client (reauth on by default)."""
    return authed_client(_app(app_factory, conn), conn, with_reauth=with_reauth)


def _make_second_user(conn, username: str = "other") -> int:
    create_user(conn, username, "OtherPass!99", enforce_policy=False)
    row = conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()
    return row["id"]


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    for lim in (
        _USER_MGMT_LIMITER,
        _USER_CREATE_LIMITER,
        _REAUTH_LIMITER,
        _PASSWORD_CHANGE_LIMITER,
        _PASSWORD_CHANGE_IP_LIMITER,
        _SESSIONS_LIST_LIMITER,
    ):
        lim.reset()
    yield
    for lim in (
        _USER_MGMT_LIMITER,
        _USER_CREATE_LIMITER,
        _REAUTH_LIMITER,
        _PASSWORD_CHANGE_LIMITER,
        _PASSWORD_CHANGE_IP_LIMITER,
        _SESSIONS_LIST_LIMITER,
    ):
        lim.reset()


class TestListUsers:
    def test_list_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/users")
        assert resp.status_code == 401

    def test_list_returns_users(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.get("/api/users")
        assert resp.status_code == 200
        body = resp.json()
        assert "users" in body
        assert "current" in body
        assert body["current"] == "admin"
        assert len(body["users"]) == 1
        assert body["users"][0]["username"] == "admin"


class TestCreateUser:
    def test_create_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/users", json={"username": "newadmin", "password": "ValidPass!99"})
        assert resp.status_code == 401

    def test_create_user_happy_path(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "newadmin", "password": "ValidPass!99"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "username": "newadmin"}
        row = conn.execute("SELECT id FROM admin_users WHERE username='newadmin'").fetchone()
        assert row is not None

    def test_create_user_short_username_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "ab", "password": "ValidPass!99"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_username"

    def test_create_user_long_username_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "a" * 65, "password": "ValidPass!99"})
        assert resp.status_code == 400

    def test_create_user_weak_password_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "validname", "password": "short"})
        assert resp.status_code == 400
        assert "issues" in resp.json()

    def test_create_user_duplicate_returns_409(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "admin", "password": "ValidPass!99"})
        assert resp.status_code == 409


class TestDeleteUser:
    def test_delete_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        admin_id = conn.execute("SELECT id FROM admin_users WHERE username='admin'").fetchone()[
            "id"
        ]
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(f"/api/users/{admin_id}")
        assert resp.status_code == 401

    def test_delete_without_reauth_returns_403_reauth_required(
        self, app_factory, authed_client, conn
    ):
        """A valid session with no recent reauth ticket cannot delete users."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=False)
        other_id = _make_second_user(conn)
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "reauth_required"
        assert body["reauth_required"] is True
        row = conn.execute("SELECT id FROM admin_users WHERE id=?", (other_id,)).fetchone()
        assert row is not None

    def test_delete_with_expired_ticket_returns_403_reauth_required(
        self, app_factory, authed_client, conn, freezer
    ):
        """A ticket older than the reauth window must not authorise delete."""
        from datetime import timedelta

        from mediaman.web.auth.reauth import reauth_window_seconds

        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        other_id = _make_second_user(conn)
        freezer.tick(timedelta(seconds=reauth_window_seconds() + 5))
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 403
        assert resp.json()["reauth_required"] is True

    def test_delete_self_returns_400(self, app_factory, authed_client, conn):
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        admin_id = conn.execute("SELECT id FROM admin_users WHERE username='admin'").fetchone()[
            "id"
        ]
        resp = client.delete(f"/api/users/{admin_id}")
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_delete_other_user_happy_path(self, app_factory, authed_client, conn):
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        other_id = _make_second_user(conn)
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = conn.execute("SELECT id FROM admin_users WHERE id=?", (other_id,)).fetchone()
        assert row is None

    def test_successful_delete_writes_exactly_one_audit_row(self, app_factory, authed_client, conn):
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        other_id = _make_second_user(conn)
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 200
        rows = conn.execute(
            "SELECT action, actor FROM audit_log WHERE action = 'sec:user.deleted'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "admin"


class TestChangePassword:
    def test_change_password_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/api/users/change-password", json={"old_password": "x", "new_password": "y"}
        )
        assert resp.status_code == 401

    def test_change_password_happy_path(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert authenticate(conn, "admin", "NewStrongPass!99") is True

    def test_change_password_wrong_old_returns_403(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "wrongpassword", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "wrong_password"

    def test_change_password_same_as_old_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "password1234"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "same_password"

    def test_change_password_weak_new_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "abc"},
        )
        assert resp.status_code == 400
        assert "issues" in resp.json()

    def test_change_password_per_actor_throttle_independent_of_ip(
        self, app_factory, authed_client, conn
    ):
        """M-2: per-actor throttle fires on 30+ wrong attempts from one user.

        The IP cap is the same shape, so this also exercises the AND
        composition — but here we verify the actor bucket trips first."""
        client = _client(app_factory, authed_client, conn)

        cap = _PASSWORD_CHANGE_LIMITER._max_in_window
        for _ in range(cap):
            client.post(
                "/api/users/change-password",
                json={"old_password": "wrong", "new_password": "NewStrongPass!99"},
            )

        # Past the cap → 429.
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "wrong", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 429

    def test_change_password_per_ip_throttle_independent_of_actor(
        self, app_factory, authed_client, conn
    ):
        """M-2: a single attacker IP cycling through users still hits the IP cap.

        We can't easily change ``admin`` mid-test (the session is bound to
        it), so we simulate the per-IP exhaustion by clearing the per-actor
        bucket between attempts — leaving only the per-IP bucket as the
        gating limiter."""
        client = _client(app_factory, authed_client, conn)

        cap = _PASSWORD_CHANGE_IP_LIMITER._max_in_window
        for _ in range(cap):
            # Reset only the per-actor bucket — keep the per-IP bucket
            # accumulating so the IP cap is the one that trips.
            _PASSWORD_CHANGE_LIMITER.reset()
            client.post(
                "/api/users/change-password",
                json={"old_password": "wrong", "new_password": "NewStrongPass!99"},
            )

        # Per-IP bucket is now exhausted; even with a fresh per-actor
        # bucket, the request must be 429.
        _PASSWORD_CHANGE_LIMITER.reset()
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "wrong", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 429


class TestListSessions:
    def test_sessions_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/users/sessions")
        assert resp.status_code == 401

    def test_sessions_returns_list(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.get("/api/users/sessions")
        assert resp.status_code == 200
        assert "sessions" in resp.json()
        assert len(resp.json()["sessions"]) >= 1

    def test_sessions_response_does_not_leak_ip_or_fingerprint(
        self, app_factory, authed_client, conn
    ):
        """Finding 14 / HIGH: ``issued_ip`` and ``fingerprint`` must NOT appear.

        The fingerprint exposes the SHA-256 prefix of UA + the /24 of the
        original IP, which is exactly what an attacker holding a stolen
        cookie would need to forge a matching fingerprint header.  The
        issued_ip would otherwise let any cookie thief dox the legitimate
        owner of the session.
        """
        client = _client(app_factory, authed_client, conn)
        resp = client.get("/api/users/sessions")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) >= 1
        for row in sessions:
            assert "issued_ip" not in row, "issued_ip leaks the user's IP — must be stripped"
            assert "fingerprint" not in row, (
                "fingerprint leaks the SHA-256/24-prefix material — must be stripped"
            )
        # The safe key set is exactly what the route exposes today.
        assert set(sessions[0].keys()) == {"created_at", "expires_at", "last_used_at"}

    def test_sessions_rate_limited_after_cap(self, app_factory, authed_client, conn):
        """Finding 16 / MEDIUM: per-actor cap stops a cookie thief from
        polling this endpoint to detect the legitimate user signing in."""
        client = _client(app_factory, authed_client, conn)

        # Burn the burst cap directly via the limiter so we don't have to
        # fire 30 HTTP requests per test.
        for _ in range(_SESSIONS_LIST_LIMITER._max_in_window):
            _SESSIONS_LIST_LIMITER.check("admin")

        resp = client.get("/api/users/sessions")
        assert resp.status_code == 429
        assert resp.json()["ok"] is False


class TestRevokeOtherSessions:
    def test_revoke_others_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/users/sessions/revoke-others")
        assert resp.status_code == 401

    def test_revoke_others_happy_path(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
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
