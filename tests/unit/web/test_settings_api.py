"""Tests for settings API endpoints, specifically GET /api/plex/libraries."""

from __future__ import annotations

import shutil
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import encrypt_value
from mediaman.services.rate_limit.instances import (
    SETTINGS_TEST_LIMITER,
    SETTINGS_WRITE_LIMITER,
)
from mediaman.web.routes.settings import _TEST_CACHE, router
from tests.helpers.factories import insert_settings


@pytest.fixture(autouse=True)
def _reset_limiters():
    """Reset shared admin limiters between tests so suite ordering does
    not cause spurious 429s. Each test starts with a clean budget. The
    service-test result cache is reset for the same reason — a stale
    cached payload would mask a tester result the next test asserts on."""
    SETTINGS_WRITE_LIMITER.reset()
    SETTINGS_TEST_LIMITER.reset()
    _TEST_CACHE.clear()
    yield
    SETTINGS_WRITE_LIMITER.reset()
    SETTINGS_TEST_LIMITER.reset()
    _TEST_CACHE.clear()


def _app(app_factory, conn):
    return app_factory(router, conn=conn)


def _client(app_factory, authed_client, conn, *, with_reauth: bool = True):
    """Shorthand: build the app + an admin client. ``with_reauth`` defaults
    to True because the bulk of these tests exercise sensitive settings
    writes; the reauth-gate tests opt out explicitly."""
    return authed_client(_app(app_factory, conn), conn, with_reauth=with_reauth)


class TestPlexLibrariesEndpoint:
    def test_returns_libraries_from_plex(self, app_factory, authed_client, conn, secret_key):
        """Happy path: Plex is configured and reachable."""
        # Store Plex settings in the test DB.
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        insert_settings(conn, plex_url="http://plex:32400", updated_at=now)
        encrypted_token = encrypt_value("fake-token", secret_key, conn=conn)
        insert_settings(conn, plex_token=encrypted_token, encrypted=1, updated_at=now)

        client = _client(app_factory, authed_client, conn)

        fake_libraries = [
            {"id": "1", "type": "movie", "title": "Movies"},
            {"id": "2", "type": "show", "title": "TV Shows"},
        ]

        with patch("mediaman.web.routes.settings.build_plex_from_db") as mock_build:
            mock_client = MagicMock()
            mock_client.get_libraries.return_value = fake_libraries
            mock_build.return_value = mock_client

            resp = client.get("/api/plex/libraries")

        assert resp.status_code == 200
        data = resp.json()
        assert data["libraries"] == fake_libraries
        assert "error" not in data

    def test_returns_error_when_plex_not_configured(self, app_factory, authed_client, conn):
        """No Plex settings in DB — returns empty list with an error message."""
        client = _client(app_factory, authed_client, conn)

        resp = client.get("/api/plex/libraries")

        assert resp.status_code == 200
        data = resp.json()
        assert data["libraries"] == []
        assert "error" in data
        assert data["error"]  # non-empty error string

    def test_returns_error_on_plex_exception(self, app_factory, authed_client, conn, secret_key):
        """PlexClient raises — returns empty list with error message."""
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        insert_settings(conn, plex_url="http://plex:32400", updated_at=now)
        encrypted_token = encrypt_value("fake-token", secret_key, conn=conn)
        insert_settings(conn, plex_token=encrypted_token, encrypted=1, updated_at=now)

        client = _client(app_factory, authed_client, conn)

        with patch("mediaman.web.routes.settings.build_plex_from_db") as mock_build:
            import requests as _requests

            mock_client = MagicMock()
            mock_client.get_libraries.side_effect = _requests.ConnectionError("unreachable")
            mock_build.return_value = mock_client

            resp = client.get("/api/plex/libraries")

        assert resp.status_code == 200
        data = resp.json()
        assert data["libraries"] == []
        assert data["error"] == "fetch_failed"

    def test_requires_auth(self, app_factory, conn):
        """Unauthenticated request is rejected with 401."""
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/plex/libraries")

        assert resp.status_code == 401


class TestDiskUsageAPI:
    def test_returns_disk_usage_for_valid_path(self, app_factory, authed_client, conn):
        """Happy path: returns total_bytes, used_bytes, free_bytes, usage_pct."""
        client = _client(app_factory, authed_client, conn)

        fake_usage = type(shutil.disk_usage("/"))(1000, 400, 600)

        with patch(
            "mediaman.web.routes.settings.shutil.disk_usage",
            return_value=fake_usage,
        ):
            resp = client.get("/api/settings/disk-usage?path=/media/movies")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_bytes"] == 1000
        assert data["used_bytes"] == 400
        assert data["free_bytes"] == 600
        assert data["usage_pct"] == 40.0

    def test_returns_error_for_missing_path_param(self, app_factory, authed_client, conn):
        """No path param — expects 400."""
        client = _client(app_factory, authed_client, conn)

        resp = client.get("/api/settings/disk-usage")

        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_rejects_path_outside_allowlist(self, app_factory, authed_client, conn):
        """Paths outside the allow-list are refused with a generic 404 (not 403)
        so the endpoint cannot be used as a path-existence oracle."""
        client = _client(app_factory, authed_client, conn)

        resp = client.get("/api/settings/disk-usage?path=/nonexistent")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_returns_error_for_nonexistent_allowlisted_path(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        """When the path IS in the allow-list but stat fails, response is generic 404."""
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/nonexistent-but-allowed")
        client = _client(app_factory, authed_client, conn)

        with patch(
            "mediaman.web.routes.settings.shutil.disk_usage",
            side_effect=FileNotFoundError("not found"),
        ):
            resp = client.get("/api/settings/disk-usage?path=/nonexistent-but-allowed")

        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_rejects_symlink_path(self, app_factory, authed_client, conn, tmp_path, monkeypatch):
        """A path that is or contains a symlink is rejected with a generic 404."""
        # Create a real dir inside an allowed root candidate, then a symlink to it.
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)

        # Make the parent tmp_path an allowed root.
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        client = _client(app_factory, authed_client, conn)

        resp = client.get(f"/api/settings/disk-usage?path={link}")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_requires_auth(self, app_factory, conn):
        """Unauthenticated request is rejected with 401."""
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/settings/disk-usage?path=/media/movies")

        assert resp.status_code == 401


class TestSettingsPutSsrfGuard:
    """PUT /api/settings must refuse URLs that point at cloud metadata
    or link-local addresses. LAN addresses stay allowed — that is the
    common self-hosted deployment."""

    def test_rejects_aws_metadata_url(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)

        resp = client.put(
            "/api/settings",
            json={"radarr_url": "http://169.254.169.254/latest/meta-data/"},
        )

        assert resp.status_code == 400
        assert "blocked" in resp.json().get("error", "").lower()

    def test_rejects_gcp_metadata_hostname(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)

        resp = client.put(
            "/api/settings",
            json={"sonarr_url": "http://metadata.google.internal/"},
        )

        assert resp.status_code == 400

    def test_rejects_file_scheme(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)

        resp = client.put(
            "/api/settings",
            json={"plex_url": "file:///etc/passwd"},
        )

        # file:// is now caught at the Pydantic model layer (422) before
        # the route's URL validation (400) has a chance to run. Either
        # status signals rejection — what matters is it is not 200.
        assert resp.status_code in (400, 422)

    def test_allows_lan_address(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)

        resp = client.put(
            "/api/settings",
            json={"radarr_url": "http://192.168.1.50:7878"},
        )

        assert resp.status_code == 200


class TestSettingsPutPersistsEveryDeclaredKey:
    """PUT /api/settings must persist every key declared in SettingsUpdate.

    This is the regression guard for C11: previously, keys missing from
    SettingsUpdate were silently dropped by Pydantic before reaching the
    route handler, so the UI's save returned 200 but changed nothing.
    """

    # Non-secret, non-URL fields that round-trip as plain strings / ints / bools.
    _PLAIN_FIELDS: dict = {
        "nzbget_username": "nzbuser",
        "mailgun_domain": "mg.example.com",
        "mailgun_from_address": "no-reply@example.com",
        "scan_day": "Monday",
        "scan_time": "03:00",
        "scan_timezone": "Europe/London",
        "library_sync_interval": 30,
        "min_age_days": 30,
        "inactivity_days": 60,
        "grace_days": 7,
        "dry_run": True,
        "suggestions_enabled": False,
        "openai_model": "gpt-5.4-mini",
    }

    # URL fields validated for http(s) scheme.
    _URL_FIELDS: dict = {
        "plex_url": "http://plex.lan:32400",
        "plex_public_url": "http://plex.example.com:32400",
        "sonarr_url": "http://sonarr.lan:8989",
        "sonarr_public_url": "http://sonarr.example.com",
        "radarr_url": "http://radarr.lan:7878",
        "radarr_public_url": "http://radarr.example.com",
        "nzbget_url": "http://nzbget.lan:6789",
        "nzbget_public_url": "http://nzbget.example.com",
        "base_url": "https://media.example.com",
    }

    # Secret fields — stored encrypted, returned as "****" by GET.
    _SECRET_FIELDS: dict = {
        "plex_token": "fake-plex-token-1234",
        "sonarr_api_key": "sonarr-key-abcd",
        "radarr_api_key": "radarr-key-efgh",
        "nzbget_password": "nzbpassword",
        "mailgun_api_key": "mg-key-ijkl",
        "tmdb_api_key": "tmdb-key-mnop",
        "tmdb_read_token": "tmdb-read-qrst",
        "openai_api_key": "sk-openai-uvwx",
        "omdb_api_key": "omdb-key-yz01",
    }

    def _put_and_get(self, app_factory, authed_client, conn, payload: dict):
        client = _client(app_factory, authed_client, conn)
        put_resp = client.put("/api/settings", json=payload)
        assert put_resp.status_code == 200, put_resp.json()
        get_resp = client.get("/api/settings")
        assert get_resp.status_code == 200
        return get_resp.json()

    def test_put_persists_plain_fields(self, app_factory, authed_client, conn):
        """All plain (non-secret, non-URL) keys round-trip correctly."""
        data = self._put_and_get(app_factory, authed_client, conn, self._PLAIN_FIELDS)
        for key, expected in self._PLAIN_FIELDS.items():
            assert key in data, f"key {key!r} missing from GET response"
            assert data[key] == expected, f"{key}: expected {expected!r}, got {data[key]!r}"

    def test_put_persists_url_fields(self, app_factory, authed_client, conn):
        """All URL-shaped settings keys round-trip correctly."""
        data = self._put_and_get(app_factory, authed_client, conn, self._URL_FIELDS)
        for key, expected in self._URL_FIELDS.items():
            assert key in data, f"key {key!r} missing from GET response"
            assert data[key] == expected, f"{key}: expected {expected!r}, got {data[key]!r}"

    def test_put_persists_secret_fields_as_masked(self, app_factory, authed_client, conn):
        """Secret keys are stored; GET returns '****' for each non-empty one."""
        data = self._put_and_get(app_factory, authed_client, conn, self._SECRET_FIELDS)
        for key in self._SECRET_FIELDS:
            assert key in data, f"secret key {key!r} missing from GET response"
            assert data[key] == "****", (
                f"secret key {key!r} should be masked as '****' in GET, got {data[key]!r}"
            )

    def test_put_persists_plex_libraries(self, app_factory, authed_client, conn):
        """plex_libraries (a list) round-trips correctly."""
        payload = {"plex_libraries": ["1", "2", "3"]}
        data = self._put_and_get(app_factory, authed_client, conn, payload)
        assert data.get("plex_libraries") == ["1", "2", "3"]

    def test_put_persists_disk_thresholds(self, app_factory, authed_client, conn):
        """disk_thresholds (nested {lib_id: {path, threshold}}) round-trips."""
        payload = {
            "disk_thresholds": {
                "1": {"path": "/media/movies", "threshold": 85},
                "2": {"path": "/media/anime", "threshold": 90},
            }
        }
        data = self._put_and_get(app_factory, authed_client, conn, payload)
        assert data.get("disk_thresholds") == payload["disk_thresholds"]

    def test_put_rejects_unknown_key_with_422(self, app_factory, authed_client, conn):
        """Unknown key must be rejected at the Pydantic layer (HTTP 422)."""
        client = _client(app_factory, authed_client, conn)
        resp = client.put("/api/settings", json={"not_a_real_setting": "oops"})
        assert resp.status_code == 422

    def test_put_rejects_crlf_in_plain_string(self, app_factory, authed_client, conn):
        """CR/LF in a plain string field must be rejected (HTTP 422)."""
        client = _client(app_factory, authed_client, conn)
        resp = client.put(
            "/api/settings",
            json={"scan_day": "Monday\r\nX-Injected: evil"},
        )
        assert resp.status_code == 422

    def test_put_rejects_crlf_in_api_key(self, app_factory, authed_client, conn):
        """CR/LF in a secret field must be rejected (HTTP 422)."""
        client = _client(app_factory, authed_client, conn)
        resp = client.put(
            "/api/settings",
            json={"openai_api_key": "sk-valid\nevil"},
        )
        assert resp.status_code == 422

    def test_put_rejects_invalid_timezone(self, app_factory, authed_client, conn):
        """An unrecognised IANA timezone must be rejected (HTTP 422)."""
        client = _client(app_factory, authed_client, conn)
        resp = client.put(
            "/api/settings",
            json={"scan_timezone": "Moon/FarSide"},
        )
        assert resp.status_code == 422

    def test_put_rejects_openai_model_outside_allowlist(self, app_factory, authed_client, conn):
        """openai_model must reject anything outside the fixed allowlist (HTTP 422).

        Defence-in-depth — the UI ``<select>`` is the primary constraint
        but the API cannot trust the client.
        """
        client = _client(app_factory, authed_client, conn)
        resp = client.put("/api/settings", json={"openai_model": "gpt-evil"})
        assert resp.status_code == 422

    def test_put_with_only_model_change_preserves_existing_api_key(
        self, app_factory, authed_client, conn
    ):
        """Saving only the model with the API-key field as '****' must NOT clobber
        the stored OpenAI key.

        The settings page renders the saved key as the ``****`` sentinel and
        the write layer skips that row when the posted value equals the
        sentinel (repository/settings.py:204-206). This test pins that the
        invariant survives the new model field being saved alongside.
        """
        client = _client(app_factory, authed_client, conn)

        # Seed an existing OpenAI key.
        seed = client.put(
            "/api/settings",
            json={"openai_api_key": "sk-original-key-do-not-clobber"},
        )
        assert seed.status_code == 200, seed.json()

        # Change ONLY the model; the API-key field still posts the sentinel.
        change = client.put(
            "/api/settings",
            json={"openai_model": "gpt-5.4-mini", "openai_api_key": "****"},
        )
        assert change.status_code == 200, change.json()

        # The stored key must still decrypt to the original value, not be
        # blanked or overwritten with the sentinel.
        from mediaman.services.openai.client import get_openai_key

        config = client.app.state.config  # type: ignore[attr-defined]
        stored = get_openai_key(conn, secret_key=config.secret_key)
        assert stored == "sk-original-key-do-not-clobber"

        # And the model must have been persisted.
        get_resp = client.get("/api/settings")
        assert get_resp.status_code == 200
        assert get_resp.json().get("openai_model") == "gpt-5.4-mini"

    def test_put_rejects_library_sync_interval_out_of_range(self, app_factory, authed_client, conn):
        """library_sync_interval outside 0–1440 minutes must be rejected (HTTP 422)."""
        client = _client(app_factory, authed_client, conn)
        resp = client.put("/api/settings", json={"library_sync_interval": 1441})
        assert resp.status_code == 422


class TestApiTestServiceOpenAiTmdbOmdb:
    """api_test_service for openai/tmdb/omdb must use SafeHTTPClient and
    validate API-key character set before placing the key in a header."""

    def _store_setting(self, conn, key, value):
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=0, updated_at=excluded.updated_at",
            (key, value, now),
        )
        conn.commit()

    def test_openai_missing_key_returns_error(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/settings/test/openai")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "required" in resp.json()["error"].lower()

    def test_tmdb_missing_key_returns_error(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/settings/test/tmdb")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_omdb_missing_key_returns_error(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/settings/test/omdb")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_openai_invalid_key_charset_rejected(self, app_factory, authed_client, conn):
        """Non-ASCII characters in an OpenAI key must be caught before the header is set."""
        self._store_setting(conn, "openai_api_key", "sk-\x00evil")
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/settings/test/openai")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "auth_failed" in data["error"]

    def test_tmdb_invalid_key_charset_rejected(self, app_factory, authed_client, conn):
        self._store_setting(conn, "tmdb_read_token", "bad\nevil")
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/settings/test/tmdb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "auth_failed" in data["error"]

    def test_omdb_invalid_key_charset_rejected(self, app_factory, authed_client, conn):
        self._store_setting(conn, "omdb_api_key", "bad\x00key")
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/settings/test/omdb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "auth_failed" in data["error"]

    def test_openai_success_via_safe_http_client(self, app_factory, authed_client, conn):
        """A 200 from the SafeHTTPClient returns ok=True."""
        self._store_setting(conn, "openai_api_key", "sk-validkey1234")
        client = _client(app_factory, authed_client, conn)

        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp._content = b"{}"
        mock_resp._content_consumed = True
        mock_resp.headers = {}

        with patch("mediaman.services.infra.http.client._dispatch", return_value=mock_resp):
            resp = client.post("/api/settings/test/openai")

        assert resp.json()["ok"] is True

    def test_openai_auth_failure_classified(self, app_factory, authed_client, conn):
        """A 401 from the backend must be classified as auth_failed."""
        self._store_setting(conn, "openai_api_key", "sk-badkey9999")
        client = _client(app_factory, authed_client, conn)

        from unittest.mock import patch

        from mediaman.services.infra import SafeHTTPError

        with patch(
            "mediaman.services.infra.http.client._dispatch",
            side_effect=SafeHTTPError(401, "Unauthorized", "https://api.openai.com/v1/models"),
        ):
            resp = client.post("/api/settings/test/openai")

        data = resp.json()
        assert data["ok"] is False
        assert "auth_failed" in data["error"]

    def test_openai_connection_error_classified(self, app_factory, authed_client, conn):
        """A transport error must be classified as connection_refused."""
        self._store_setting(conn, "openai_api_key", "sk-goodkey1234")
        client = _client(app_factory, authed_client, conn)

        from unittest.mock import patch

        from mediaman.services.infra import SafeHTTPError

        with patch(
            "mediaman.services.infra.http.client._dispatch",
            side_effect=SafeHTTPError(
                0, "transport error: ConnectionError", "https://api.openai.com/v1/models"
            ),
        ):
            resp = client.post("/api/settings/test/openai")

        data = resp.json()
        assert data["ok"] is False
        assert "connection_refused" in data["error"]

    def test_openai_ssrf_classified(self, app_factory, authed_client, conn):
        """SSRF guard refusal must surface as ssrf_refused."""
        self._store_setting(conn, "openai_api_key", "sk-goodkey1234")
        client = _client(app_factory, authed_client, conn)

        from unittest.mock import patch

        from mediaman.services.infra import SafeHTTPError

        with patch(
            "mediaman.services.infra.http.client._dispatch",
            side_effect=SafeHTTPError(
                0, "refused by SSRF guard", "https://api.openai.com/v1/models"
            ),
        ):
            resp = client.post("/api/settings/test/openai")

        data = resp.json()
        assert data["ok"] is False
        assert "ssrf_refused" in data["error"]


class TestSettingsTestServiceRateLimit:
    """Service-test endpoint must be admin-keyed rate-limited so a leaked
    session cookie cannot chain test calls to flood Plex / Mailgun."""

    def test_eleventh_call_in_window_is_429(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        # Limiter is 10/min — eleventh call must be throttled.
        # Clear the result cache between calls so each one hits the
        # tester (and therefore the limiter) rather than returning the
        # cached 200 — the cache short-circuits limiter accounting by
        # design.
        for _ in range(10):
            _TEST_CACHE.clear()
            resp = client.post("/api/settings/test/openai")
            assert resp.status_code == 200
        _TEST_CACHE.clear()
        resp = client.post("/api/settings/test/openai")
        assert resp.status_code == 429


class TestSettingsTestServiceTimeout:
    """Each tester runs under a hard wall-clock cap so an unreachable
    Plex cannot pin the request thread for 35+ seconds."""

    def test_long_running_tester_returns_timeout(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        import threading

        from mediaman.web.routes import settings as settings_module

        def slow_tester(_settings):
            # Block long enough that the production timeout (0.1 s) fires
            # before this returns, but exit promptly so the ThreadPoolExecutor
            # shutdown does not stall the test.  0.3 s is 3× the cap —
            # generous margin without adding real wall-clock cost.
            threading.Event().wait(timeout=0.3)
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True})

        monkeypatch.setattr(settings_module, "_TESTER_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setitem(settings_module._SERVICE_TESTERS, "plex", slow_tester)

        client = _client(app_factory, authed_client, conn)

        resp = client.post("/api/settings/test/plex")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["error"] == "timeout"


class TestSettingsTestServiceScopedDecryption:
    """A single-service test must NOT decrypt every other secret in
    the DB. The route restricts ``_load_settings`` to the keys that
    tester actually needs."""

    def test_openai_test_does_not_touch_plex_token(
        self, app_factory, authed_client, conn, secret_key, monkeypatch
    ):
        """Patch the decrypt function and assert it's only called for
        ``openai_api_key`` when the openai tester runs."""
        from datetime import datetime

        # Seed a real encrypted plex_token + openai_api_key so the test
        # can prove only one is touched.
        now = datetime.now(UTC).isoformat()
        ct_plex = encrypt_value("plex-secret", secret_key, conn=conn, aad=b"plex_token")
        ct_openai = encrypt_value("sk-openai", secret_key, conn=conn, aad=b"openai_api_key")
        insert_settings(conn, plex_token=ct_plex, encrypted=1, updated_at=now)
        insert_settings(conn, openai_api_key=ct_openai, encrypted=1, updated_at=now)

        # Decryption now happens inside web.repository.settings — patch the
        # name on that module so the recording callable intercepts the call.
        from mediaman.web.repository import settings as settings_repo

        seen_keys: list[bytes] = []
        original_decrypt = settings_repo.decrypt_value

        def recording_decrypt(value, sk, *, conn=None, aad=None):
            if aad is not None:
                seen_keys.append(aad)
            return original_decrypt(value, sk, conn=conn, aad=aad)

        monkeypatch.setattr(settings_repo, "decrypt_value", recording_decrypt)

        client = _client(app_factory, authed_client, conn)

        # Stub the actual SafeHTTP call so we don't hit the network.
        with patch(
            "mediaman.services.infra.http.client._dispatch",
            return_value=MagicMock(status_code=200, headers={}, content=b"{}"),
        ):
            resp = client.post("/api/settings/test/openai")
        assert resp.status_code == 200

        # The openai tester only needs openai_api_key. plex_token
        # MUST NOT have been decrypted.
        assert b"plex_token" not in seen_keys, (
            f"plex_token should not be decrypted by an openai test; saw aad={seen_keys}"
        )
        assert b"openai_api_key" in seen_keys


class TestSettingsLoadDistinguishesDecryptFromMissing:
    """``_load_settings`` must surface ``ConfigDecryptError`` for rows that
    exist but cannot be decrypted, so callers can distinguish "secret
    rotated" from "secret never set"."""

    def test_decrypt_failure_raises_config_decrypt_error(self, conn, secret_key):
        from datetime import datetime

        from mediaman.services.infra import ConfigDecryptError
        from mediaman.web.routes.settings import _load_settings

        # Write a ciphertext encrypted under one key, then attempt to
        # decrypt under a different key.
        other_key = "fedcba9876543210" * 4
        ct = encrypt_value("plex-secret", other_key, conn=conn, aad=b"plex_token")

        now = datetime.now(UTC).isoformat()
        insert_settings(conn, plex_token=ct, encrypted=1, updated_at=now)

        with pytest.raises(ConfigDecryptError):
            _load_settings(conn, secret_key, keys={"plex_token"})


class TestSettingsApiGetSkipsDecryption:
    """GET /api/settings should never attempt to decrypt secrets — they
    are masked as '****' regardless of plaintext, so the decryption
    cost is wasted and a needless plaintext exposure window."""

    def test_get_does_not_decrypt_secrets(
        self, app_factory, authed_client, conn, secret_key, monkeypatch
    ):
        from datetime import datetime

        # Decryption lives in web.repository.settings; patch the symbol there.
        from mediaman.web.repository import settings as settings_repo

        now = datetime.now(UTC).isoformat()
        ct = encrypt_value("very-secret", secret_key, conn=conn, aad=b"plex_token")
        insert_settings(conn, plex_token=ct, encrypted=1, updated_at=now)

        seen: list[bytes] = []
        original = settings_repo.decrypt_value

        def recording(value, sk, *, conn=None, aad=None):
            if aad is not None:
                seen.append(aad)
            return original(value, sk, conn=conn, aad=aad)

        monkeypatch.setattr(settings_repo, "decrypt_value", recording)

        client = _client(app_factory, authed_client, conn)

        resp = client.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json().get("plex_token") == "****"
        assert b"plex_token" not in seen, (
            f"GET /api/settings should not decrypt secrets; saw {seen}"
        )


class TestSettingsClearSentinel:
    """Sending the ``__CLEAR__`` sentinel for a secret field deletes the
    row — without it, a stored credential cannot be erased through the
    UI."""

    def test_clear_sentinel_deletes_secret_row(self, app_factory, authed_client, conn):
        # Seed a stored secret.
        client = _client(app_factory, authed_client, conn, with_reauth=True)

        resp = client.put("/api/settings", json={"plex_token": "real-token-1234"})
        assert resp.status_code == 200
        row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
        assert row is not None

        # Clear it.
        resp = client.put("/api/settings", json={"plex_token": "__CLEAR__"})
        assert resp.status_code == 200
        row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
        assert row is None

    def test_clear_sentinel_requires_reauth(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn, with_reauth=False)

        resp = client.put("/api/settings", json={"plex_token": "__CLEAR__"})
        assert resp.status_code == 403


class TestSettingsThrottleAuditLog:
    """When the settings-write rate limiter fires, an audit row must be
    written so operators can see the throttled attempt — not just a log
    line in app stdout."""

    def test_throttled_write_records_security_event(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)

        # Burn through the burst window (20/min).
        for _ in range(20):
            resp = client.put("/api/settings", json={"scan_day": "monday"})
            assert resp.status_code == 200

        resp = client.put("/api/settings", json={"scan_day": "monday"})
        assert resp.status_code == 429

        rows = conn.execute(
            "SELECT action, detail FROM audit_log WHERE action = 'sec:settings.write.throttled'"
        ).fetchall()
        assert rows, "throttle path must write a security_event audit row"


class TestSsrfBlockedLogScrubs:
    """SSRF-blocked URLs must not be logged verbatim — userinfo and
    query strings can carry credentials and the candidate is
    user-controlled."""

    def test_userinfo_stripped_from_log(self, app_factory, authed_client, conn, caplog):
        from mediaman.web.routes import settings as settings_module

        # Force the URL into the SSRF-blocked path by stubbing
        # is_safe_outbound_url to refuse it.
        with patch.object(settings_module, "is_safe_outbound_url", return_value=False):
            client = _client(app_factory, authed_client, conn)
            with caplog.at_level("WARNING"):
                resp = client.put(
                    "/api/settings",
                    json={"radarr_url": "http://admin:s3cr3t@evil.example.com/path?api_key=leaked"},
                )
        assert resp.status_code == 400

        joined = " ".join(rec.getMessage() for rec in caplog.records)
        # Neither the userinfo nor the query string can appear verbatim.
        assert "s3cr3t" not in joined
        assert "leaked" not in joined
        # Host should still be logged for triage value.
        assert "evil.example.com" in joined


class TestAutoAbandonSetting:
    """auto_abandon_enabled is the new boolean replacing the three count-based knobs."""

    def test_round_trip_enabled(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        put_resp = client.put("/api/settings", json={"auto_abandon_enabled": "true"})
        assert put_resp.status_code == 200, put_resp.json()
        response = client.get("/api/settings")
        assert response.status_code == 200
        # PUT stores the string "true"; json.loads("true") == True, so the GET
        # response deserialises the stored row back to a JSON boolean.
        assert response.json().get("auto_abandon_enabled") is True

    def test_default_when_unset(self, app_factory, authed_client, conn):
        # Fresh DB, no setting written — the route either omits the key or returns a falsy default.
        client = _client(app_factory, authed_client, conn)
        response = client.get("/api/settings")
        assert response.status_code == 200
        val = response.json().get("auto_abandon_enabled")
        assert val in (None, False, "false", "0", ""), f"unexpected default: {val!r}"

    def test_deprecated_keys_are_rejected(self, app_factory, authed_client, conn):
        # The three legacy keys were removed from SettingsUpdate (extra="forbid"),
        # so sending them now returns 422 — they are never persisted.
        client = _client(app_factory, authed_client, conn)
        for key in (
            "abandon_search_visible_at",
            "abandon_search_escalate_at",
            "abandon_search_auto_multiplier",
        ):
            response = client.put("/api/settings", json={key: 99})
            assert response.status_code == 422, (
                f"{key} should be rejected (422) after removal from SettingsUpdate"
            )
