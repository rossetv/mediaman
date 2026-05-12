"""Reauth-gate and audit-in-transaction tests for the settings PUT route.

Covers M7 (sensitive settings require recent reauth) and M27 (audit
write happens in the same transaction as the settings mutation; if the
audit fails for sensitive keys the whole write rolls back).
"""

from __future__ import annotations

import pytest

from mediaman.services.rate_limit.instances import (
    SETTINGS_WRITE_LIMITER as _SETTINGS_WRITE_LIMITER,
)
from mediaman.web.routes.settings import SECRET_FIELDS, SENSITIVE_KEYS, router


@pytest.fixture(autouse=True)
def _clear_settings_limiter():
    _SETTINGS_WRITE_LIMITER.reset()
    yield
    _SETTINGS_WRITE_LIMITER.reset()


def _client(app_factory, authed_client, conn, *, with_reauth: bool = False):
    """Return a TestClient. Reauth NOT granted by default — opt-in only."""
    app = app_factory(router, conn=conn)
    return authed_client(app, conn, with_reauth=with_reauth)


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
    def test_secret_field_without_reauth_is_403(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={"plex_token": "leak-this-elsewhere"},
        )
        assert resp.status_code == 403
        assert resp.json()["reauth_required"] is True

        # Critically — the value was NOT persisted.
        row = conn.execute("SELECT value FROM settings WHERE key = 'plex_token'").fetchone()
        assert row is None

    def test_url_field_without_reauth_is_403(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={"sonarr_url": "https://attacker.example.com"},
        )
        assert resp.status_code == 403
        row = conn.execute("SELECT value FROM settings WHERE key = 'sonarr_url'").fetchone()
        assert row is None

    def test_base_url_change_without_reauth_is_403(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={"base_url": "https://attacker.example.com"},
        )
        assert resp.status_code == 403

    def test_mailgun_change_without_reauth_is_403(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn, with_reauth=False)

        resp = client.put(
            "/api/settings",
            json={"mailgun_from_address": "bad@example.com"},
        )
        assert resp.status_code == 403

    def test_low_impact_key_works_without_reauth(self, app_factory, authed_client, conn):
        """A scan-day tweak does not need a fresh reauth."""
        client = _client(app_factory, authed_client, conn, with_reauth=False)

        resp = client.put("/api/settings", json={"scan_day": "monday"})
        assert resp.status_code == 200
        row = conn.execute("SELECT value FROM settings WHERE key = 'scan_day'").fetchone()
        assert row["value"] == "monday"

    def test_mixed_payload_rejected_atomically(self, app_factory, authed_client, conn):
        """A body with one sensitive + one non-sensitive key must reject
        the WHOLE request — not partial-write the harmless half."""
        client = _client(app_factory, authed_client, conn, with_reauth=False)

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

    def test_unchanged_secret_placeholder_does_not_require_reauth(
        self, app_factory, authed_client, conn
    ):
        """The "****" sentinel for unchanged secrets should not flip the
        gate on — the route already ignores the value, so demanding a
        reauth would be friction with no security benefit."""
        client = _client(app_factory, authed_client, conn, with_reauth=False)

        resp = client.put("/api/settings", json={"plex_token": "****"})
        assert resp.status_code == 200

    def test_with_reauth_succeeds(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn, with_reauth=True)

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
    def test_settings_write_records_security_audit_row(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn, with_reauth=True)

        resp = client.put("/api/settings", json={"plex_url": "https://plex.example.com"})
        assert resp.status_code == 200

        rows = conn.execute(
            "SELECT action, detail FROM audit_log WHERE action = 'sec:settings.write'"
        ).fetchall()
        assert len(rows) == 1
        # Sensitive-keys list is included in the detail blob.
        assert "plex_url" in rows[0]["detail"]
        assert "sensitive_keys" in rows[0]["detail"]

    def test_audit_failure_rolls_back_sensitive_write(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        """If the audit insert blows up, the settings change must roll back.

        A "settings changed but no one knows" outcome is exactly the
        scenario the audit system exists to prevent — fail closed.
        """
        client = _client(app_factory, authed_client, conn, with_reauth=True)

        import sqlite3 as _sqlite3

        from mediaman.core import audit as audit_module

        def boom(*_args, **_kwargs):
            raise _sqlite3.OperationalError("simulated audit failure")

        # Audit is called via lazy ``from mediaman.core.audit import
        # security_event_or_raise`` inside write_settings — patch the
        # source so the patched function intercepts the call.
        monkeypatch.setattr(audit_module, "security_event_or_raise", boom)

        resp = client.put("/api/settings", json={"plex_url": "https://plex.example.com"})
        assert resp.status_code == 500
        # The settings row must NOT have been persisted — the
        # transaction rolled back with the failed audit insert.
        row = conn.execute("SELECT value FROM settings WHERE key = 'plex_url'").fetchone()
        assert row is None
