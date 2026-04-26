"""Tests for settings API endpoints, specifically GET /api/plex/libraries."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.crypto import encrypt_value
from mediaman.db import init_db, set_connection
from mediaman.web.routes.settings import router


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


def _auth_client(app: FastAPI, conn) -> TestClient:
    """Return a TestClient with a valid admin session cookie set."""
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
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
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
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
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
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
        assert "Failed to fetch Plex libraries" in data["error"]

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

        fake_usage = {"total_bytes": 1000, "used_bytes": 400, "free_bytes": 600}

        with patch(
            "mediaman.web.routes.settings.get_disk_usage",
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
            "mediaman.web.routes.settings.get_disk_usage",
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
        """disk_thresholds (a dict) round-trips correctly."""
        payload = {"disk_thresholds": {"/media": 85, "/data": 90}}
        data = self._put_and_get(conn, secret_key, payload)
        assert data.get("disk_thresholds") == {"/media": 85, "/data": 90}

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
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
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
