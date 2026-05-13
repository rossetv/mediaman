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

    def test_invalid_email_returns_400_and_keeps_existing(self, app_factory, authed_client, conn):
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

    def test_wrong_password_returns_403_and_keeps_existing(self, app_factory, authed_client, conn):
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

    def test_patch_email_writes_audit_row_on_success(self, app_factory, authed_client, conn):
        """Successful email update writes a sec:user.email_updated audit row."""
        client = _client(app_factory, authed_client, conn)
        resp = client.patch(
            "/api/users/me/email",
            json={"email": "ops@example.com"},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 200
        row = conn.execute(
            "SELECT action, actor, detail FROM audit_log WHERE action = 'sec:user.email_updated'"
        ).fetchone()
        assert row is not None
        assert row["actor"] == "admin"
        assert '"cleared":false' in row["detail"]

    def test_patch_clear_email_writes_audit_with_cleared_true(
        self, app_factory, authed_client, conn
    ):
        """Clearing the email writes a sec:user.email_updated row with cleared:true."""
        client = _client(app_factory, authed_client, conn)
        # First set an address, then clear it.
        set_user_email(conn, "admin", "ops@example.com")
        resp = client.patch(
            "/api/users/me/email",
            json={"email": ""},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 200
        row = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:user.email_updated'"
        ).fetchone()
        assert row is not None
        assert '"cleared":true' in row["detail"]

    def test_patch_email_writes_audit_row_on_reauth_failure(self, app_factory, authed_client, conn):
        """A wrong password triggers a sec:user.email_update.reauth_failed audit row."""
        client = _client(app_factory, authed_client, conn)
        resp = client.patch(
            "/api/users/me/email",
            json={"email": "attacker@evil.test"},
            headers={"X-Confirm-Password": "wrongpassword"},
        )
        assert resp.status_code == 403
        row = conn.execute(
            "SELECT action, actor FROM audit_log"
            " WHERE action = 'sec:user.email_update.reauth_failed'"
        ).fetchone()
        assert row is not None
        assert row["actor"] == "admin"

    def test_patch_email_writes_audit_row_on_rate_limit(self, app_factory, authed_client, conn):
        """Tripping the rate limiter writes a sec:user.email_update.rate_limited row."""
        client = _client(app_factory, authed_client, conn)
        # Exhaust the per-actor bucket so the route call trips the limiter.
        for _ in range(_USER_MGMT_LIMITER._max_in_window):
            _USER_MGMT_LIMITER.check("admin")

        resp = client.patch(
            "/api/users/me/email",
            json={"email": "ops@example.com"},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 429
        row = conn.execute(
            "SELECT action, actor FROM audit_log"
            " WHERE action = 'sec:user.email_update.rate_limited'"
        ).fetchone()
        assert row is not None
        assert row["actor"] == "admin"

    def test_update_email_body_rejects_extra_fields(self, app_factory, authed_client, conn):
        """Stowaway fields in the request body must return HTTP 422."""
        client = _client(app_factory, authed_client, conn)
        resp = client.patch(
            "/api/users/me/email",
            json={"email": "ops@example.com", "stowaway": "x"},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 422

    def test_update_email_body_rejects_overlong(self, app_factory, authed_client, conn):
        """An address over 320 characters must return HTTP 422."""
        client = _client(app_factory, authed_client, conn)
        long_email = "a" * 321 + "@example.com"
        resp = client.patch(
            "/api/users/me/email",
            json={"email": long_email},
            headers={"X-Confirm-Password": "password1234"},
        )
        assert resp.status_code == 422
