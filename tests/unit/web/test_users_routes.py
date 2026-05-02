"""Targeted tests for :mod:`mediaman.web.routes.users` — supplement to test_users.py.

test_users.py already has thorough coverage of all routes. This file adds:
  - user-creation rate limiting (separate limiter with lower cap)
  - revoke-others re-issues a fresh session cookie
  - change-password issues a fresh session cookie on success

These behaviours require inspecting Set-Cookie headers which the existing
tests don't assert on.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.reauth import grant_recent_reauth
from mediaman.auth.session import create_session, create_user, validate_session
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    _REAUTH_LIMITER,
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


def _auth_client(app: FastAPI, conn, *, with_reauth: bool = True) -> tuple[TestClient, str]:
    """Return (client, token) for a freshly-minted admin session.

    When *with_reauth* is True (the default), the session is also
    granted a fresh recent-reauth ticket so privilege-establishing
    endpoints are allowed.
    """
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    if with_reauth:
        grant_recent_reauth(conn, token, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client, token


class TestUserCreateRateLimit:
    """The user-creation limiter (3 per hour) fires independently of _USER_MGMT_LIMITER."""

    def setup_method(self):
        for lim in (
            _USER_CREATE_LIMITER,
            _USER_MGMT_LIMITER,
            _REAUTH_LIMITER,
            _PASSWORD_CHANGE_LIMITER,
            _PASSWORD_CHANGE_IP_LIMITER,
        ):
            lim.reset()

    def test_user_create_throttled_after_cap(self, db_path, secret_key):
        """After _USER_CREATE_LIMITER._max_in_window requests, 429 is returned."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client, _ = _auth_client(app, conn)

        cap = _USER_CREATE_LIMITER._max_in_window
        for i in range(cap):
            # Different usernames to avoid 409 conflicts
            client.post(
                "/api/users",
                json={"username": f"user{i:03d}", "password": "ValidPass!99"},
            )

        resp = client.post(
            "/api/users",
            json={"username": "throttled_user", "password": "ValidPass!99"},
        )
        assert resp.status_code == 429
        assert resp.json()["ok"] is False


class TestRevokeOthersReissueCookie:
    """POST /api/users/sessions/revoke-others must issue a fresh session cookie."""

    def test_revoke_others_sets_new_session_cookie(self, db_path, secret_key):
        """The response includes a new session_token cookie."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client, old_token = _auth_client(app, conn)

        resp = client.post("/api/users/sessions/revoke-others")
        assert resp.status_code == 200

        # A new cookie must have been issued
        new_cookie = resp.cookies.get("session_token")
        assert new_cookie is not None
        # The new cookie must differ from the original (old session was destroyed)
        assert new_cookie != old_token

    def test_revoke_others_new_session_is_valid(self, db_path, secret_key):
        """The re-issued session cookie is a valid, working session."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client, _ = _auth_client(app, conn)

        resp = client.post("/api/users/sessions/revoke-others")
        new_cookie = resp.cookies.get("session_token")

        assert validate_session(conn, new_cookie) == "admin"

    def test_revoke_others_exactly_one_session_remains(self, db_path, secret_key):
        """After revocation there is exactly one active session (the new one)."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client, _ = _auth_client(app, conn)
        # Add a second session to ensure revocation fires
        create_session(conn, "admin")

        client.post("/api/users/sessions/revoke-others")

        count = conn.execute(
            "SELECT COUNT(*) FROM admin_sessions WHERE username='admin'"
        ).fetchone()[0]
        assert count == 1


class TestChangePasswordCookie:
    """POST /api/users/change-password must re-issue a session cookie on success."""

    def test_change_password_sets_new_session_cookie(self, db_path, secret_key):
        """A successful password change returns a new session_token cookie."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client, old_token = _auth_client(app, conn)

        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 200
        new_cookie = resp.cookies.get("session_token")
        assert new_cookie is not None
        assert new_cookie != old_token

    def test_change_password_new_session_is_valid(self, db_path, secret_key):
        """The re-issued session cookie after a password change is a valid session."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client, _ = _auth_client(app, conn)

        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewStrongPass!99"},
        )
        new_cookie = resp.cookies.get("session_token")
        assert validate_session(conn, new_cookie) == "admin"
