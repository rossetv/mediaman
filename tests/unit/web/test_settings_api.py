"""Tests for settings API endpoints, specifically GET /api/plex/libraries."""

from __future__ import annotations

import shutil
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import encrypt_value
from mediaman.db import init_db, set_connection
from mediaman.services.infra.rate_limits import (
    SETTINGS_TEST_LIMITER,
    SETTINGS_WRITE_LIMITER,
)
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.settings import _TEST_CACHE, router


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


def _make_app(conn, secret_key: str) -> FastAPI:
    """Build a minimal FastAPI app wired to *conn* for testing.

    Bypasses the full lifespan/config machinery so tests run without env vars.
    """
    app = FastAPI()
    app.include_router(router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    # Override the module-level get_db() so all route code uses the test DB.
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn, *, with_reauth: bool = True) -> TestClient:
    """Return a TestClient with a valid admin session cookie set.

    When *with_reauth* is True (the default for legacy tests that don't
    care about the gate), a recent-reauth ticket is granted for the
    session so PUT ``/api/settings`` against sensitive keys is allowed.
    Tests that exercise the reauth gate itself pass ``with_reauth=False``.
    """
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


class TestPlexLibrariesEndpoint:
    def test_returns_libraries_from_plex(self, conn, secret_key):
        """Happy path: Plex is configured and reachable."""
        # Store Plex settings in the test DB.
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            ("plex_url", "http://plex:32400", now),
        )
        encrypted_token = encrypt_value("fake-token", secret_key, conn=conn)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 1, ?)",
            ("plex_token", encrypted_token, now),
        )
        conn.commit()

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

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

    def test_returns_error_when_plex_not_configured(self, conn, secret_key):
        """No Plex settings in DB — returns empty list with an error message."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.get("/api/plex/libraries")

        assert resp.status_code == 200
        data = resp.json()
        assert data["libraries"] == []
        assert "error" in data
        assert data["error"]  # non-empty error string

    def test_returns_error_on_plex_exception(self, conn, secret_key):
        """PlexClient raises — returns empty list with error message."""
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            ("plex_url", "http://plex:32400", now),
        )
        encrypted_token = encrypt_value("fake-token", secret_key, conn=conn)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 1, ?)",
            ("plex_token", encrypted_token, now),
        )
        conn.commit()

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        with patch("mediaman.web.routes.settings.build_plex_from_db") as mock_build:
            mock_client = MagicMock()
            mock_client.get_libraries.side_effect = ConnectionError("unreachable")
            mock_build.return_value = mock_client

            resp = client.get("/api/plex/libraries")

        assert resp.status_code == 200
        data = resp.json()
        assert data["libraries"] == []
        assert data["error"] == "fetch_failed"

    def test_requires_auth(self, conn, secret_key):
        """Unauthenticated request is rejected with 401."""
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/plex/libraries")

        assert resp.status_code == 401


class TestDiskUsageAPI:
    def test_returns_disk_usage_for_valid_path(self, conn, secret_key):
        """Happy path: returns total_bytes, used_bytes, free_bytes, usage_pct."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

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

    def test_returns_error_for_missing_path_param(self, conn, secret_key):
        """No path param — expects 400."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.get("/api/settings/disk-usage")

        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_rejects_path_outside_allowlist(self, conn, secret_key):
        """Paths outside the allow-list are refused with a generic 404 (not 403)
        so the endpoint cannot be used as a path-existence oracle."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.get("/api/settings/disk-usage?path=/nonexistent")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_returns_error_for_nonexistent_allowlisted_path(self, conn, secret_key, monkeypatch):
        """When the path IS in the allow-list but stat fails, response is generic 404."""
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/nonexistent-but-allowed")
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        with patch(
            "mediaman.web.routes.settings.shutil.disk_usage",
            side_effect=FileNotFoundError("not found"),
        ):
            resp = client.get("/api/settings/disk-usage?path=/nonexistent-but-allowed")

        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_rejects_symlink_path(self, conn, secret_key, tmp_path, monkeypatch):
        """A path that is or contains a symlink is rejected with a generic 404."""
        # Create a real dir inside an allowed root candidate, then a symlink to it.
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)

        # Make the parent tmp_path an allowed root.
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.get(f"/api/settings/disk-usage?path={link}")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_requires_auth(self, conn, secret_key):
        """Unauthenticated request is rejected with 401."""
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/settings/disk-usage?path=/media/movies")

        assert resp.status_code == 401


class TestSettingsPutSsrfGuard:
    """PUT /api/settings must refuse URLs that point at cloud metadata
    or link-local addresses. LAN addresses stay allowed — that is the
    common self-hosted deployment."""

    def test_rejects_aws_metadata_url(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.put(
            "/api/settings",
            json={"radarr_url": "http://169.254.169.254/latest/meta-data/"},
        )

        assert resp.status_code == 400
        assert "blocked" in resp.json().get("error", "").lower()

    def test_rejects_gcp_metadata_hostname(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.put(
            "/api/settings",
            json={"sonarr_url": "http://metadata.google.internal/"},
        )

        assert resp.status_code == 400

    def test_rejects_file_scheme(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.put(
            "/api/settings",
            json={"plex_url": "file:///etc/passwd"},
        )

        # file:// is now caught at the Pydantic model layer (422) before
        # the route's URL validation (400) has a chance to run. Either
        # status signals rejection — what matters is it is not 200.
        assert resp.status_code in (400, 422)

    def test_allows_lan_address(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

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

    def _put_and_get(self, conn, secret_key, payload: dict):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        put_resp = client.put("/api/settings", json=payload)
        assert put_resp.status_code == 200, put_resp.json()
        get_resp = client.get("/api/settings")
        assert get_resp.status_code == 200
        return get_resp.json()

    def test_put_persists_plain_fields(self, conn, secret_key):
        """All plain (non-secret, non-URL) keys round-trip correctly."""
        data = self._put_and_get(conn, secret_key, self._PLAIN_FIELDS)
        for key, expected in self._PLAIN_FIELDS.items():
            assert key in data, f"key {key!r} missing from GET response"
            assert data[key] == expected, f"{key}: expected {expected!r}, got {data[key]!r}"

    def test_put_persists_url_fields(self, conn, secret_key):
        """All URL-shaped settings keys round-trip correctly."""
        data = self._put_and_get(conn, secret_key, self._URL_FIELDS)
        for key, expected in self._URL_FIELDS.items():
            assert key in data, f"key {key!r} missing from GET response"
            assert data[key] == expected, f"{key}: expected {expected!r}, got {data[key]!r}"

    def test_put_persists_secret_fields_as_masked(self, conn, secret_key):
        """Secret keys are stored; GET returns '****' for each non-empty one."""
        data = self._put_and_get(conn, secret_key, self._SECRET_FIELDS)
        for key in self._SECRET_FIELDS:
            assert key in data, f"secret key {key!r} missing from GET response"
            assert data[key] == "****", (
                f"secret key {key!r} should be masked as '****' in GET, got {data[key]!r}"
            )

    def test_put_persists_plex_libraries(self, conn, secret_key):
        """plex_libraries (a list) round-trips correctly."""
        payload = {"plex_libraries": ["1", "2", "3"]}
        data = self._put_and_get(conn, secret_key, payload)
        assert data.get("plex_libraries") == ["1", "2", "3"]

    def test_put_persists_disk_thresholds(self, conn, secret_key):
        """disk_thresholds (nested {lib_id: {path, threshold}}) round-trips."""
        payload = {
            "disk_thresholds": {
                "1": {"path": "/media/movies", "threshold": 85},
                "2": {"path": "/media/anime", "threshold": 90},
            }
        }
        data = self._put_and_get(conn, secret_key, payload)
        assert data.get("disk_thresholds") == payload["disk_thresholds"]

    def test_put_rejects_unknown_key_with_422(self, conn, secret_key):
        """Unknown key must be rejected at the Pydantic layer (HTTP 422)."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.put("/api/settings", json={"not_a_real_setting": "oops"})
        assert resp.status_code == 422

    def test_put_rejects_crlf_in_plain_string(self, conn, secret_key):
        """CR/LF in a plain string field must be rejected (HTTP 422)."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.put(
            "/api/settings",
            json={"scan_day": "Monday\r\nX-Injected: evil"},
        )
        assert resp.status_code == 422

    def test_put_rejects_crlf_in_api_key(self, conn, secret_key):
        """CR/LF in a secret field must be rejected (HTTP 422)."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.put(
            "/api/settings",
            json={"openai_api_key": "sk-valid\nevil"},
        )
        assert resp.status_code == 422

    def test_put_rejects_invalid_timezone(self, conn, secret_key):
        """An unrecognised IANA timezone must be rejected (HTTP 422)."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.put(
            "/api/settings",
            json={"scan_timezone": "Moon/FarSide"},
        )
        assert resp.status_code == 422

    def test_put_rejects_library_sync_interval_out_of_range(self, conn, secret_key):
        """library_sync_interval outside 0–1440 minutes must be rejected (HTTP 422)."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.put("/api/settings", json={"library_sync_interval": 1441})
        assert resp.status_code == 422


class TestApiTestServiceOpenAiTmdbOmdb:
    """api_test_service for openai/tmdb/omdb must use SafeHTTPClient and
    validate API-key character set before placing the key in a header."""

    def _client(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        return _auth_client(app, conn)

    def _store_setting(self, conn, key, value):
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=0, updated_at=excluded.updated_at",
            (key, value, now),
        )
        conn.commit()

    def test_openai_missing_key_returns_error(self, conn, secret_key):
        client = self._client(conn, secret_key)
        resp = client.post("/api/settings/test/openai")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "required" in resp.json()["error"].lower()

    def test_tmdb_missing_key_returns_error(self, conn, secret_key):
        client = self._client(conn, secret_key)
        resp = client.post("/api/settings/test/tmdb")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_omdb_missing_key_returns_error(self, conn, secret_key):
        client = self._client(conn, secret_key)
        resp = client.post("/api/settings/test/omdb")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_openai_invalid_key_charset_rejected(self, conn, secret_key):
        """Non-ASCII characters in an OpenAI key must be caught before the header is set."""
        self._store_setting(conn, "openai_api_key", "sk-\x00evil")
        client = self._client(conn, secret_key)
        resp = client.post("/api/settings/test/openai")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "auth_failed" in data["error"]

    def test_tmdb_invalid_key_charset_rejected(self, conn, secret_key):
        self._store_setting(conn, "tmdb_read_token", "bad\nevil")
        client = self._client(conn, secret_key)
        resp = client.post("/api/settings/test/tmdb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "auth_failed" in data["error"]

    def test_omdb_invalid_key_charset_rejected(self, conn, secret_key):
        self._store_setting(conn, "omdb_api_key", "bad\x00key")
        client = self._client(conn, secret_key)
        resp = client.post("/api/settings/test/omdb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "auth_failed" in data["error"]

    def test_openai_success_via_safe_http_client(self, conn, secret_key):
        """A 200 from the SafeHTTPClient returns ok=True."""
        self._store_setting(conn, "openai_api_key", "sk-validkey1234")
        client = self._client(conn, secret_key)

        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp._content = b"{}"
        mock_resp._content_consumed = True
        mock_resp.headers = {}

        with patch("mediaman.services.infra.http_client._dispatch", return_value=mock_resp):
            resp = client.post("/api/settings/test/openai")

        assert resp.json()["ok"] is True

    def test_openai_auth_failure_classified(self, conn, secret_key):
        """A 401 from the backend must be classified as auth_failed."""
        self._store_setting(conn, "openai_api_key", "sk-badkey9999")
        client = self._client(conn, secret_key)

        from unittest.mock import patch

        from mediaman.services.infra.http_client import SafeHTTPError

        with patch(
            "mediaman.services.infra.http_client._dispatch",
            side_effect=SafeHTTPError(401, "Unauthorized", "https://api.openai.com/v1/models"),
        ):
            resp = client.post("/api/settings/test/openai")

        data = resp.json()
        assert data["ok"] is False
        assert "auth_failed" in data["error"]

    def test_openai_connection_error_classified(self, conn, secret_key):
        """A transport error must be classified as connection_refused."""
        self._store_setting(conn, "openai_api_key", "sk-goodkey1234")
        client = self._client(conn, secret_key)

        from unittest.mock import patch

        from mediaman.services.infra.http_client import SafeHTTPError

        with patch(
            "mediaman.services.infra.http_client._dispatch",
            side_effect=SafeHTTPError(
                0, "transport error: ConnectionError", "https://api.openai.com/v1/models"
            ),
        ):
            resp = client.post("/api/settings/test/openai")

        data = resp.json()
        assert data["ok"] is False
        assert "connection_refused" in data["error"]

    def test_openai_ssrf_classified(self, conn, secret_key):
        """SSRF guard refusal must surface as ssrf_refused."""
        self._store_setting(conn, "openai_api_key", "sk-goodkey1234")
        client = self._client(conn, secret_key)

        from unittest.mock import patch

        from mediaman.services.infra.http_client import SafeHTTPError

        with patch(
            "mediaman.services.infra.http_client._dispatch",
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

    def test_eleventh_call_in_window_is_429(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
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

    def test_long_running_tester_returns_timeout(self, conn, secret_key, monkeypatch):
        import time

        from mediaman.web.routes import settings as settings_module

        def slow_tester(_settings):
            time.sleep(2.0)
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True})

        monkeypatch.setattr(settings_module, "_TESTER_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setitem(settings_module._SERVICE_TESTERS, "plex", slow_tester)

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.post("/api/settings/test/plex")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["error"] == "timeout"


class TestSettingsTestServiceScopedDecryption:
    """A single-service test must NOT decrypt every other secret in
    the DB. The route restricts ``_load_settings`` to the keys that
    tester actually needs."""

    def test_openai_test_does_not_touch_plex_token(self, conn, secret_key, monkeypatch):
        """Patch the decrypt function and assert it's only called for
        ``openai_api_key`` when the openai tester runs."""
        from datetime import datetime

        # Seed a real encrypted plex_token + openai_api_key so the test
        # can prove only one is touched.
        now = datetime.now(UTC).isoformat()
        ct_plex = encrypt_value("plex-secret", secret_key, conn=conn, aad=b"plex_token")
        ct_openai = encrypt_value("sk-openai", secret_key, conn=conn, aad=b"openai_api_key")
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 1, ?)",
            ("plex_token", ct_plex, now),
        )
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 1, ?)",
            ("openai_api_key", ct_openai, now),
        )
        conn.commit()

        from mediaman.web.routes import settings as settings_module

        seen_keys: list[bytes] = []
        original_decrypt = settings_module.decrypt_value

        def recording_decrypt(value, sk, *, conn=None, aad=None):
            if aad is not None:
                seen_keys.append(aad)
            return original_decrypt(value, sk, conn=conn, aad=aad)

        monkeypatch.setattr(settings_module, "decrypt_value", recording_decrypt)

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        # Stub the actual SafeHTTP call so we don't hit the network.
        with patch(
            "mediaman.services.infra.http_client._dispatch",
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

        from mediaman.services.infra.settings_reader import ConfigDecryptError
        from mediaman.web.routes.settings import _load_settings

        # Write a ciphertext encrypted under one key, then attempt to
        # decrypt under a different key.
        other_key = "fedcba9876543210" * 4
        ct = encrypt_value("plex-secret", other_key, conn=conn, aad=b"plex_token")

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 1, ?)",
            ("plex_token", ct, now),
        )
        conn.commit()

        with pytest.raises(ConfigDecryptError):
            _load_settings(conn, secret_key, keys={"plex_token"})


class TestSettingsApiGetSkipsDecryption:
    """GET /api/settings should never attempt to decrypt secrets — they
    are masked as '****' regardless of plaintext, so the decryption
    cost is wasted and a needless plaintext exposure window."""

    def test_get_does_not_decrypt_secrets(self, conn, secret_key, monkeypatch):
        from datetime import datetime

        from mediaman.web.routes import settings as settings_module

        now = datetime.now(UTC).isoformat()
        ct = encrypt_value("very-secret", secret_key, conn=conn, aad=b"plex_token")
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 1, ?)",
            ("plex_token", ct, now),
        )
        conn.commit()

        seen: list[bytes] = []
        original = settings_module.decrypt_value

        def recording(value, sk, *, conn=None, aad=None):
            if aad is not None:
                seen.append(aad)
            return original(value, sk, conn=conn, aad=aad)

        monkeypatch.setattr(settings_module, "decrypt_value", recording)

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

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

    def test_clear_sentinel_deletes_secret_row(self, conn, secret_key):
        # Seed a stored secret.
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn, with_reauth=True)

        resp = client.put("/api/settings", json={"plex_token": "real-token-1234"})
        assert resp.status_code == 200
        row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
        assert row is not None

        # Clear it.
        resp = client.put("/api/settings", json={"plex_token": "__CLEAR__"})
        assert resp.status_code == 200
        row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
        assert row is None

    def test_clear_sentinel_requires_reauth(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn, with_reauth=False)

        resp = client.put("/api/settings", json={"plex_token": "__CLEAR__"})
        assert resp.status_code == 403


class TestSettingsThrottleAuditLog:
    """When the settings-write rate limiter fires, an audit row must be
    written so operators can see the throttled attempt — not just a log
    line in app stdout."""

    def test_throttled_write_records_security_event(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

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

    def test_userinfo_stripped_from_log(self, conn, secret_key, caplog):
        from mediaman.web.routes import settings as settings_module

        # Force the URL into the SSRF-blocked path by stubbing
        # is_safe_outbound_url to refuse it.
        with patch.object(settings_module, "is_safe_outbound_url", return_value=False):
            app = _make_app(conn, secret_key)
            client = _auth_client(app, conn)
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

    def test_round_trip_enabled(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        put_resp = client.put("/api/settings", json={"auto_abandon_enabled": "true"})
        assert put_resp.status_code == 200, put_resp.json()
        response = client.get("/api/settings")
        assert response.status_code == 200
        # PUT stores the string "true"; json.loads("true") == True, so the GET
        # response deserialises the stored row back to a JSON boolean.
        assert response.json().get("auto_abandon_enabled") is True

    def test_default_when_unset(self, conn, secret_key):
        # Fresh DB, no setting written — the route either omits the key or returns a falsy default.
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        response = client.get("/api/settings")
        assert response.status_code == 200
        val = response.json().get("auto_abandon_enabled")
        assert val in (None, False, "false", "0", ""), f"unexpected default: {val!r}"

    def test_deprecated_keys_are_rejected(self, conn, secret_key):
        # The three legacy keys were removed from SettingsUpdate (extra="forbid"),
        # so sending them now returns 422 — they are never persisted.
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        for key in (
            "abandon_search_visible_at",
            "abandon_search_escalate_at",
            "abandon_search_auto_multiplier",
        ):
            response = client.put("/api/settings", json={key: 99})
            assert response.status_code == 422, (
                f"{key} should be rejected (422) after removal from SettingsUpdate"
            )
