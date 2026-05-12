"""Targeted tests for :mod:`mediaman.web.routes.users` — supplement to test_users.py.

test_users.py already has thorough coverage of all routes. This file adds:
  - user-creation rate limiting (separate limiter with lower cap)
  - revoke-others re-issues a fresh session cookie
  - change-password issues a fresh session cookie on success

These behaviours require inspecting Set-Cookie headers which the existing
tests don't assert on.
"""

from __future__ import annotations

from mediaman.web.auth.session_store import create_session, validate_session
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    _REAUTH_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
)
from mediaman.web.routes.users import router as users_router


def _build(app_factory, authed_client, conn, *, with_reauth: bool = True):
    """Return ``(client, token)``: the shared authed_client plus the cookie
    it stored, so tests can compare against the post-rotation cookie."""
    app = app_factory(users_router, conn=conn)
    client = authed_client(app, conn, with_reauth=with_reauth)
    token = client.cookies.get("session_token")
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

    def test_user_create_throttled_after_cap(self, app_factory, authed_client, conn):
        """After _USER_CREATE_LIMITER._max_in_window requests, 429 is returned."""
        client, _ = _build(app_factory, authed_client, conn)

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

    def test_revoke_others_sets_new_session_cookie(self, app_factory, authed_client, conn):
        """The response includes a new session_token cookie."""
        client, old_token = _build(app_factory, authed_client, conn)

        resp = client.post("/api/users/sessions/revoke-others")
        assert resp.status_code == 200

        # A new cookie must have been issued
        new_cookie = resp.cookies.get("session_token")
        assert new_cookie is not None
        # The new cookie must differ from the original (old session was destroyed)
        assert new_cookie != old_token

    def test_revoke_others_new_session_is_valid(self, app_factory, authed_client, conn):
        """The re-issued session cookie is a valid, working session."""
        client, _ = _build(app_factory, authed_client, conn)

        resp = client.post("/api/users/sessions/revoke-others")
        new_cookie = resp.cookies.get("session_token")

        assert validate_session(conn, new_cookie) == "admin"

    def test_revoke_others_exactly_one_session_remains(self, app_factory, authed_client, conn):
        """After revocation there is exactly one active session (the new one)."""
        client, _ = _build(app_factory, authed_client, conn)
        # Add a second session to ensure revocation fires
        create_session(conn, "admin")

        client.post("/api/users/sessions/revoke-others")

        count = conn.execute(
            "SELECT COUNT(*) FROM admin_sessions WHERE username='admin'"
        ).fetchone()[0]
        assert count == 1


class TestChangePasswordCookie:
    """POST /api/users/change-password must re-issue a session cookie on success."""

    def test_change_password_sets_new_session_cookie(self, app_factory, authed_client, conn):
        """A successful password change returns a new session_token cookie."""
        client, old_token = _build(app_factory, authed_client, conn)

        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 200
        new_cookie = resp.cookies.get("session_token")
        assert new_cookie is not None
        assert new_cookie != old_token

    def test_change_password_new_session_is_valid(self, app_factory, authed_client, conn):
        """The re-issued session cookie after a password change is a valid session."""
        client, _ = _build(app_factory, authed_client, conn)

        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewStrongPass!99"},
        )
        new_cookie = resp.cookies.get("session_token")
        assert validate_session(conn, new_cookie) == "admin"
