"""Reauth-gate and audit-in-transaction tests for the settings PUT route.

Covers M7 (sensitive settings require recent reauth) and M27 (audit
write happens in the same transaction as the settings mutation; if the
audit fails for sensitive keys the whole write rolls back).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.services.rate_limit.instances import (
    SETTINGS_WRITE_LIMITER as _SETTINGS_WRITE_LIMITER,
)
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.settings import SECRET_FIELDS, SENSITIVE_KEYS, router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _client(app: FastAPI, conn, *, with_reauth: bool = False) -> TestClient:
    """Return a TestClient. Reauth NOT granted by default — opt-in only."""
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    if with_reauth:
        grant_recent_reauth(conn, token, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


@pytest.fixture(autouse=True)
def _clear_settings_limiter():
    _SETTINGS_WRITE_LIMITER.reset()
    yield
    _SETTINGS_WRITE_LIMITER.reset()


# ---------------------------------------------------------------------------
# Sensitive-key membership sanity check — the spec says secrets, URLs,
# mail bits, and base_url MUST be inside SENSITIVE_KEYS.
# ---------------------------------------------------------------------------


class TestSensitiveKeySetMembership:
    def test_every_secret_field_is_sensitive(self):
        for key in SECRET_FIELDS:
            assert key in SENSITIVE_KEYS, f"secret {key!r} missing from SENSITIVE_KEYS"

    def test_every_url_field_is_sensitive(self):
        for key in (
            "plex_url",
            "plex_public_url",
            "sonarr_url",
            "sonarr_public_url",
            "radarr_url",
            "radarr_public_url",
            "nzbget_url",
            "nzbget_public_url",
            "base_url",
        ):
            assert key in SENSITIVE_KEYS, f"URL {key!r} missing from SENSITIVE_KEYS"

    def test_mail_fields_are_sensitive(self):
        assert "mailgun_domain" in SENSITIVE_KEYS
        assert "mailgun_from_address" in SENSITIVE_KEYS

    def test_low_impact_keys_are_not_sensitive(self):
        """Don't gate harmless UI-only knobs behind reauth."""
        for key in ("scan_day", "scan_time", "min_age_days", "dry_run"):
            assert key not in SENSITIVE_KEYS


# ---------------------------------------------------------------------------
# M7 — sensitive settings demand recent reauth
# ---------------------------------------------------------------------------


class TestSensitiveSettingsRequireReauth:
    def test_secret_field_without_reauth_is_403(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={"plex_token": "leak-this-elsewhere"},
        )
        assert resp.status_code == 403
        assert resp.json()["reauth_required"] is True

        # Critically — the value was NOT persisted.
        row = conn.execute("SELECT value FROM settings WHERE key = 'plex_token'").fetchone()
        assert row is None

    def test_url_field_without_reauth_is_403(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={"sonarr_url": "https://attacker.example.com"},
        )
        assert resp.status_code == 403
        row = conn.execute("SELECT value FROM settings WHERE key = 'sonarr_url'").fetchone()
        assert row is None

    def test_base_url_change_without_reauth_is_403(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={"base_url": "https://attacker.example.com"},
        )
        assert resp.status_code == 403

    def test_mailgun_change_without_reauth_is_403(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={"mailgun_from_address": "bad@example.com"},
        )
        assert resp.status_code == 403

    def test_low_impact_key_works_without_reauth(self, conn, secret_key):
        """A scan-day tweak does not need a fresh reauth."""
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=False)

        resp = client.put("/api/settings", json={"scan_day": "monday"})
        assert resp.status_code == 200
        row = conn.execute("SELECT value FROM settings WHERE key = 'scan_day'").fetchone()
        assert row["value"] == "monday"

    def test_mixed_payload_rejected_atomically(self, conn, secret_key):
        """A body with one sensitive + one non-sensitive key must reject
        the WHOLE request — not partial-write the harmless half."""
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={
                "scan_day": "monday",
                "plex_token": "should-not-land",
            },
        )
        assert resp.status_code == 403
        # Both keys must be absent from the DB.
        for key in ("scan_day", "plex_token"):
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            assert row is None, f"key {key!r} should not have been written"

    def test_unchanged_secret_placeholder_does_not_require_reauth(self, conn, secret_key):
        """The "****" sentinel for unchanged secrets should not flip the
        gate on — the route already ignores the value, so demanding a
        reauth would be friction with no security benefit."""
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=False)

        resp = client.put("/api/settings", json={"plex_token": "****"})
        assert resp.status_code == 200

    def test_with_reauth_succeeds(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=True)

        resp = client.put(
            "/api/settings",
            json={"plex_token": "real-token-1234"},
        )
        assert resp.status_code == 200
        row = conn.execute(
            "SELECT value, encrypted FROM settings WHERE key = 'plex_token'"
        ).fetchone()
        assert row is not None
        assert row["encrypted"] == 1


# ---------------------------------------------------------------------------
# M27 — audit-in-transaction
# ---------------------------------------------------------------------------


class TestSettingsAuditInTransaction:
    def test_settings_write_records_security_audit_row(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=True)

        resp = client.put("/api/settings", json={"plex_url": "https://plex.example.com"})
        assert resp.status_code == 200

        rows = conn.execute(
            "SELECT action, detail FROM audit_log WHERE action = 'sec:settings.write'"
        ).fetchall()
        assert len(rows) == 1
        # Sensitive-keys list is included in the detail blob.
        assert "plex_url" in rows[0]["detail"]
        assert "sensitive_keys" in rows[0]["detail"]

    def test_audit_failure_rolls_back_sensitive_write(self, conn, secret_key, monkeypatch):
        """If the audit insert blows up, the settings change must roll back.

        A "settings changed but no one knows" outcome is exactly the
        scenario the audit system exists to prevent — fail closed.
        """
        app = _make_app(conn, secret_key)
        client = _client(app, conn, with_reauth=True)

        # The audit insert now lives inside web.repository.settings; patch it
        # at the source so the in-transaction insert blows up.
        import mediaman.core.audit as audit_module

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated audit failure")

        monkeypatch.setattr(audit_module, "security_event_or_raise", boom)

        resp = client.put("/api/settings", json={"plex_url": "https://plex.example.com"})
        assert resp.status_code == 500
        # The settings row must NOT have been persisted — the
        # transaction rolled back with the failed audit insert.
        row = conn.execute("SELECT value FROM settings WHERE key = 'plex_url'").fetchone()
        assert row is None
