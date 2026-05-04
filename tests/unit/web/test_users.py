"""Tests for user management API routes (list, create, delete, change-password, sessions)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.reauth import grant_recent_reauth
from mediaman.auth.session import authenticate, create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    _REAUTH_LIMITER,
    _SESSIONS_LIST_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
)
from mediaman.web.routes.users import router as users_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(users_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn, *, with_reauth: bool = True) -> TestClient:
    """Return a TestClient with a logged-in admin session.

    When *with_reauth* is True (the default), a fresh recent-reauth
    ticket is granted on the session so privilege-establishing
    endpoints (admin creation, unlock) are allowed. Tests that exercise
    the reauth gate itself pass ``with_reauth=False``.
    """
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    if with_reauth:
        grant_recent_reauth(conn, token, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


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
        assert resp.json()["error"] == "invalid_username"

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
        assert resp.json()["error"] == "password_required"

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
        assert resp.json()["error"] == "use_header"


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
        assert resp.json()["error"] == "wrong_password"

    def test_change_password_same_as_old_returns_400(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "password1234"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "same_password"

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

    def test_change_password_per_actor_throttle_independent_of_ip(self, db_path, secret_key):
        """M-2: per-actor throttle fires on 30+ wrong attempts from one user.

        The IP cap is the same shape, so this also exercises the AND
        composition — but here we verify the actor bucket trips first."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

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

    def test_change_password_per_ip_throttle_independent_of_actor(self, db_path, secret_key):
        """M-2: a single attacker IP cycling through users still hits the IP cap.

        We can't easily change ``admin`` mid-test (the session is bound to
        it), so we simulate the per-IP exhaustion by clearing the per-actor
        bucket between attempts — leaving only the per-IP bucket as the
        gating limiter."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

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

    def test_sessions_response_does_not_leak_ip_or_fingerprint(self, db_path, secret_key):
        """Finding 14 / HIGH: ``issued_ip`` and ``fingerprint`` must NOT appear.

        The fingerprint exposes the SHA-256 prefix of UA + the /24 of the
        original IP, which is exactly what an attacker holding a stolen
        cookie would need to forge a matching fingerprint header.  The
        issued_ip would otherwise let any cookie thief dox the legitimate
        owner of the session.
        """
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
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

    def test_sessions_rate_limited_after_cap(self, db_path, secret_key):
        """Finding 16 / MEDIUM: per-actor cap stops a cookie thief from
        polling this endpoint to detect the legitimate user signing in."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        # Burn the burst cap directly via the limiter so we don't have to
        # fire 30 HTTP requests per test.
        for _ in range(_SESSIONS_LIST_LIMITER._max_in_window):
            _SESSIONS_LIST_LIMITER.check("admin")

        resp = client.get("/api/users/sessions")
        assert resp.status_code == 429
        assert resp.json()["ok"] is False


class TestDeleteUserBruteForceLockout:
    """Finding 13 / CRITICAL: wrong-password attempts must feed the
    ``reauth:<admin>`` namespace lockout so a stolen cookie cannot be
    used to mount a low-and-slow brute-force against ``X-Confirm-Password``.
    """

    def test_wrong_password_records_reauth_failure(self, db_path, secret_key):
        """One wrong password bumps the reauth-namespace counter to 1."""
        from mediaman.auth.reauth import REAUTH_LOCKOUT_PREFIX

        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)

        resp = client.delete(
            f"/api/users/{other_id}",
            headers={"X-Confirm-Password": "wrongpassword"},
        )
        assert resp.status_code == 403

        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            (f"{REAUTH_LOCKOUT_PREFIX}admin",),
        ).fetchone()
        assert row is not None
        assert row["failure_count"] == 1

    def test_five_wrong_passwords_lock_the_namespace(self, db_path, secret_key):
        """Five wrong passwords trip the namespace lockout — even the
        right password is then refused for the lock duration."""
        from mediaman.auth.login_lockout import check_lockout
        from mediaman.auth.reauth import REAUTH_LOCKOUT_PREFIX

        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)

        for _ in range(5):
            # Reset only the rate limiter — the namespace lockout is
            # what we are testing; the rate limiter would otherwise trip
            # at 5/min and obscure the lockout.
            _USER_MGMT_LIMITER.reset()
            resp = client.delete(
                f"/api/users/{other_id}",
                headers={"X-Confirm-Password": "wrongpassword"},
            )
            assert resp.status_code == 403

        assert check_lockout(conn, f"{REAUTH_LOCKOUT_PREFIX}admin") is True

        # Even with the correct password the delete is now refused.
        _USER_MGMT_LIMITER.reset()
        resp = client.delete(
            f"/api/users/{other_id}",
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 403
        # The target user must still exist — the delete really was refused.
        row = conn.execute("SELECT id FROM admin_users WHERE id=?", (other_id,)).fetchone()
        assert row is not None

    def test_failed_delete_does_not_bump_plain_login_counter(self, db_path, secret_key):
        """A stolen-session attacker pounding delete-user must not also
        DoS the legitimate user out of /login by polluting their plain
        ``admin`` lockout counter."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)

        for _ in range(3):
            _USER_MGMT_LIMITER.reset()
            client.delete(
                f"/api/users/{other_id}",
                headers={"X-Confirm-Password": "wrongpassword"},
            )

        plain = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = 'admin'"
        ).fetchone()
        assert plain is None


class TestDeleteUserAuditTrail:
    """Finding 15 / HIGH: failed delete attempts must leave an audit row
    so SecOps can spot a brute-force pattern from the log alone."""

    def test_reauth_failed_writes_audit_row(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)

        resp = client.delete(
            f"/api/users/{other_id}",
            headers={"X-Confirm-Password": "wrongpassword"},
        )
        assert resp.status_code == 403

        rows = conn.execute(
            "SELECT action, actor, detail FROM audit_log "
            "WHERE action = 'sec:user.delete.reauth_failed'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "admin"
        assert "actor=admin" in rows[0]["detail"]
        assert str(other_id) in rows[0]["detail"]

    def test_rate_limit_writes_audit_row(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)

        # Burn the cap.
        for _ in range(_USER_MGMT_LIMITER._max_in_window):
            _USER_MGMT_LIMITER.check("admin")

        resp = client.delete(
            f"/api/users/{other_id}",
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 429

        rows = conn.execute(
            "SELECT action, actor FROM audit_log WHERE action = 'sec:user.delete.rate_limited'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "admin"

    def test_successful_delete_writes_exactly_one_audit_row(self, db_path, secret_key):
        """Belt-and-braces — verify the success path still writes exactly
        one ``sec:user.deleted`` row (not zero, not two) so the failed-
        delete additions did not accidentally double-log success."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        other_id = _make_second_user(conn)

        resp = client.delete(
            f"/api/users/{other_id}",
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 200

        rows = conn.execute(
            "SELECT action, actor FROM audit_log WHERE action = 'sec:user.deleted'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "admin"


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
