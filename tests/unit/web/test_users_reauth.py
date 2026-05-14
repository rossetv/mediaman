"""Reauth-gate, password-change throttling, and admin-unlock tests for
:mod:`mediaman.web.routes.users`.

Covers the high-impact paths added in M6, M8, M21:
- POST /api/users without recent reauth must be rejected (M6).
- POST /api/auth/reauth establishes a recent-reauth ticket (M6).
- Password-change attempts feed the reauth-namespace lockout (M8).
- POST /api/users/{id}/unlock requires reauth and clears the lock (M21).
- Audit-in-transaction: a failing audit insert rolls back the mutation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.login_lockout import is_locked_out, record_failure
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.reauth import (
    REAUTH_LOCKOUT_PREFIX,
    has_recent_reauth,
)
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_LIMITER,
    _REAUTH_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
)
from mediaman.web.routes.users import router as users_router


def _build(app_factory, authed_client, conn, *, with_reauth: bool = False):
    """Return ``(client, token)``. Reauth NOT granted by default — opt in."""
    app = app_factory(users_router, conn=conn)
    client = authed_client(app, conn, with_reauth=with_reauth)
    token = client.cookies.get("session_token")
    return client, token


def _make_other_user(conn, username: str = "other", password: str = "OtherPass!99") -> int:
    create_user(conn, username, password, enforce_policy=False)
    return conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()["id"]


@pytest.fixture(autouse=True)
def _clear_rate_limiters():
    for lim in (
        _USER_CREATE_LIMITER,
        _USER_MGMT_LIMITER,
        _REAUTH_LIMITER,
        _PASSWORD_CHANGE_LIMITER,
    ):
        lim.reset()
    yield
    for lim in (
        _USER_CREATE_LIMITER,
        _USER_MGMT_LIMITER,
        _REAUTH_LIMITER,
        _PASSWORD_CHANGE_LIMITER,
    ):
        lim.reset()


# ---------------------------------------------------------------------------
# POST /api/users requires a recent re-authentication ticket
# ---------------------------------------------------------------------------


class TestCreateUserRequiresReauth:
    def test_create_user_without_reauth_returns_403(self, app_factory, authed_client, conn):
        """A valid session alone must NOT be enough to mint a new admin."""
        client, _ = _build(app_factory, authed_client, conn, with_reauth=False)

        resp = client.post(
            "/api/users",
            json={"username": "newadmin", "password": "ValidPass!99"},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["reauth_required"] is True
        # And critically — the user must NOT have been created.
        row = conn.execute("SELECT id FROM admin_users WHERE username='newadmin'").fetchone()
        assert row is None

    def test_create_user_with_reauth_succeeds(self, app_factory, authed_client, conn):
        client, _ = _build(app_factory, authed_client, conn, with_reauth=True)

        resp = client.post(
            "/api/users",
            json={"username": "newadmin", "password": "ValidPass!99"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# POST /api/auth/reauth — establishes the ticket; throttles failures.
# ---------------------------------------------------------------------------


class TestReauthEndpoint:
    def test_reauth_correct_password_grants_ticket(self, app_factory, authed_client, conn):
        client, token = _build(app_factory, authed_client, conn)

        resp = client.post("/api/auth/reauth", json={"password": "password1234"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "expires_in_seconds" in resp.json()
        # The ticket is now persisted for this session.
        assert has_recent_reauth(conn, token, "admin") is True

    def test_reauth_wrong_password_returns_403(self, app_factory, authed_client, conn):
        client, token = _build(app_factory, authed_client, conn)

        resp = client.post("/api/auth/reauth", json={"password": "WRONG"})
        assert resp.status_code == 403
        # No ticket was minted.
        assert has_recent_reauth(conn, token, "admin") is False

    def test_five_wrong_reauth_attempts_lock_namespace(self, app_factory, authed_client, conn):
        """M8: the reauth endpoint must throttle wrong-password attempts.

        After five failures, the reauth namespace is locked and even the
        correct password is refused for the lock duration.
        """
        client, _ = _build(app_factory, authed_client, conn)

        # Fire five wrong attempts. The 5th trips the lock.
        for _ in range(5):
            resp = client.post("/api/auth/reauth", json={"password": "WRONG"})
            assert resp.status_code == 403

        # The reauth-namespace lockout is now active.
        assert is_locked_out(conn, f"{REAUTH_LOCKOUT_PREFIX}admin") is True

        # Even the correct password is refused while the lock is in place.
        resp = client.post("/api/auth/reauth", json={"password": "password1234"})
        assert resp.status_code == 403

    def test_reauth_failures_do_not_bump_plain_login_counter(
        self, app_factory, authed_client, conn
    ):
        """A stolen-session attacker pounding /api/auth/reauth must not
        DoS the legitimate user out of /login."""
        client, _ = _build(app_factory, authed_client, conn)

        for _ in range(5):
            client.post("/api/auth/reauth", json={"password": "WRONG"})

        # The plain-login counter for "admin" is untouched.
        plain = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = 'admin'"
        ).fetchone()
        assert plain is None

    def test_reauth_endpoint_is_burst_throttled(self, app_factory, authed_client, conn):
        """The per-actor ``_REAUTH_LIMITER`` caps burst attempts.

        Set high enough that the namespace lockout is the dominant
        brute-force defence, but low enough to slow down obvious abuse.
        """
        client, _ = _build(app_factory, authed_client, conn)

        # Burn the burst cap directly via the limiter so we don't have
        # to fire 30 HTTP requests per test.
        for _ in range(_REAUTH_LIMITER._max_in_window):
            _REAUTH_LIMITER.check("admin")

        resp = client.post("/api/auth/reauth", json={"password": "password1234"})
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# change_password attempts are tracked in the reauth-namespace lockout counter
# ---------------------------------------------------------------------------


class TestChangePasswordThrottling:
    def test_five_wrong_old_passwords_lock_namespace(self, app_factory, authed_client, conn):
        """A stolen session pounding /api/users/change-password must
        trip the reauth-namespace lockout."""
        client, _ = _build(app_factory, authed_client, conn)

        for _ in range(5):
            resp = client.post(
                "/api/users/change-password",
                json={"old_password": "WRONG", "new_password": "NewPass!9912"},
            )
            assert resp.status_code == 403

        # Reauth-namespace lock is now active.
        assert is_locked_out(conn, f"{REAUTH_LOCKOUT_PREFIX}admin") is True

        # Even the correct old password is refused now.
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewPass!9912"},
        )
        assert resp.status_code == 403

    def test_change_password_burst_throttled(self, app_factory, authed_client, conn):
        """Per-actor cap: ``_PASSWORD_CHANGE_LIMITER._max_in_window``
        attempts in 60 s."""
        client, _ = _build(app_factory, authed_client, conn)

        # Burn the burst cap directly so we don't have to fire 30 HTTP
        # requests per test.
        for _ in range(_PASSWORD_CHANGE_LIMITER._max_in_window):
            _PASSWORD_CHANGE_LIMITER.check("admin")

        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewPass!9912"},
        )
        assert resp.status_code == 429

    def test_change_password_does_not_bump_plain_login_counter(
        self, app_factory, authed_client, conn
    ):
        client, _ = _build(app_factory, authed_client, conn)

        for _ in range(3):
            client.post(
                "/api/users/change-password",
                json={"old_password": "WRONG", "new_password": "NewPass!9912"},
            )

        plain = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = 'admin'"
        ).fetchone()
        assert plain is None


# ---------------------------------------------------------------------------
# POST /api/users/{id}/unlock requires a recent re-authentication ticket
# ---------------------------------------------------------------------------


class TestAdminUnlockEndpoint:
    def test_unlock_requires_auth(self, app_factory, conn):
        app = app_factory(users_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/users/1/unlock")
        assert resp.status_code == 401

    def test_unlock_requires_recent_reauth(self, app_factory, authed_client, conn):
        client, _ = _build(app_factory, authed_client, conn, with_reauth=False)
        target_id = _make_other_user(conn)

        # Lock the target by recording 5 failures.
        for _ in range(5):
            record_failure(conn, "other")

        resp = client.post(f"/api/users/{target_id}/unlock")
        assert resp.status_code == 403
        assert resp.json()["reauth_required"] is True
        # The lock is still in place.
        assert is_locked_out(conn, "other") is True

    def test_unlock_clears_the_lock(self, app_factory, authed_client, conn):
        client, _ = _build(app_factory, authed_client, conn, with_reauth=True)
        target_id = _make_other_user(conn)

        for _ in range(5):
            record_failure(conn, "other")
        assert is_locked_out(conn, "other") is True

        resp = client.post(f"/api/users/{target_id}/unlock")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["had_lock"] is True
        assert is_locked_out(conn, "other") is False

    def test_unlock_refuses_self(self, app_factory, authed_client, conn):
        client, _ = _build(app_factory, authed_client, conn, with_reauth=True)
        admin_id = conn.execute("SELECT id FROM admin_users WHERE username='admin'").fetchone()[
            "id"
        ]

        resp = client.post(f"/api/users/{admin_id}/unlock")
        assert resp.status_code == 400

    def test_unlock_unknown_id_returns_404(self, app_factory, authed_client, conn):
        client, _ = _build(app_factory, authed_client, conn, with_reauth=True)

        resp = client.post("/api/users/99999/unlock")
        assert resp.status_code == 404

    def test_unlock_writes_security_audit_row(self, app_factory, authed_client, conn):
        client, _ = _build(app_factory, authed_client, conn, with_reauth=True)
        target_id = _make_other_user(conn)

        for _ in range(5):
            record_failure(conn, "other")

        client.post(f"/api/users/{target_id}/unlock")

        rows = conn.execute(
            "SELECT action, detail FROM audit_log WHERE action = 'sec:user.unlocked'"
        ).fetchall()
        assert len(rows) == 1
        assert "actor=admin" in rows[0]["detail"]
        assert "other" in rows[0]["detail"]


# ---------------------------------------------------------------------------
# Audit fail-closed for admin creation
# ---------------------------------------------------------------------------


class TestCreateUserAuditInTransaction:
    def test_audit_failure_rolls_back_user_create(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        """If the audit insert blows up, the user-creation must roll back.

        A "user created but no audit trail" outcome IS the security
        incident the audit system is supposed to surface — so the only
        safe behaviour is to fail closed.
        """
        client, _ = _build(app_factory, authed_client, conn, with_reauth=True)

        # The audit insert lives inside create_user (in mediaman.core.audit).
        # Patch it there so the in-transaction insert blows up.
        import sqlite3 as _sqlite3

        import mediaman.core.audit as audit_module

        def boom(*_args, **_kwargs):
            raise _sqlite3.OperationalError("simulated audit failure")

        monkeypatch.setattr(audit_module, "security_event_or_raise", boom)

        resp = client.post(
            "/api/users",
            json={"username": "ghostadmin", "password": "ValidPass!99"},
        )
        assert resp.status_code == 500
        # And the user MUST NOT exist — the create rolled back with the
        # audit row.
        row = conn.execute("SELECT id FROM admin_users WHERE username = 'ghostadmin'").fetchone()
        assert row is None
