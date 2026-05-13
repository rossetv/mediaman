"""Tests for ``PATCH /api/users/me/email``.

The endpoint lets the current authenticated admin set or clear their
notification email. It mirrors the ``DELETE /api/users/{user_id}``
reauth pattern: password re-confirmation via ``X-Confirm-Password``
header, query-string credentials rejected, and the same
``_USER_MGMT_LIMITER`` namespace.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.password_hash import get_user_email, set_user_email
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


def _client(app_factory, authed_client, conn):
    return authed_client(_app(app_factory, conn), conn)


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


class TestPatchOwnEmail:
    def test_happy_path_sets_email(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.patch(
            "/api/users/me/email",
            json={"email": "ops@example.com"},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert get_user_email(conn, "admin") == "ops@example.com"

    def test_empty_email_clears_email(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        # Pre-populate so we can prove the PATCH cleared it.
        set_user_email(conn, "admin", "ops@example.com")
        assert get_user_email(conn, "admin") == "ops@example.com"

        resp = client.patch(
            "/api/users/me/email",
            json={"email": ""},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert get_user_email(conn, "admin") is None

    def test_invalid_email_returns_400_and_keeps_existing(
        self, app_factory, authed_client, conn
    ):
        client = _client(app_factory, authed_client, conn)
        set_user_email(conn, "admin", "ops@example.com")
        resp = client.patch(
            "/api/users/me/email",
            json={"email": "rossetv"},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_email"
        # Unchanged.
        assert get_user_email(conn, "admin") == "ops@example.com"

    def test_wrong_password_returns_403_and_keeps_existing(
        self, app_factory, authed_client, conn
    ):
        client = _client(app_factory, authed_client, conn)
        set_user_email(conn, "admin", "ops@example.com")
        resp = client.patch(
            "/api/users/me/email",
            json={"email": "attacker@evil.test"},
            headers={"X-Confirm-Password": "wrongpassword"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "password_required"
        # Unchanged.
        assert get_user_email(conn, "admin") == "ops@example.com"

    def test_password_in_query_string_rejected(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.patch(
            "/api/users/me/email?confirm_password=password1234",
            json={"email": "ops@example.com"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "use_header"
        assert get_user_email(conn, "admin") is None

    def test_unauthenticated_returns_401(self, app_factory, conn):
        from mediaman.web.auth.password_hash import create_user

        create_user(conn, "admin", "password1234", enforce_policy=False)
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            "/api/users/me/email",
            json={"email": "ops@example.com"},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 401
        assert get_user_email(conn, "admin") is None
