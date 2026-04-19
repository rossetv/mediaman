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
from mediaman.web.routes.settings_routes import router


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

        with patch(
            "mediaman.web.routes.settings_routes._build_plex_client"
        ) as mock_build:
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

        with patch(
            "mediaman.web.routes.settings_routes._build_plex_client"
        ) as mock_build:
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
            "mediaman.web.routes.settings_routes.get_disk_usage",
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
        """Paths outside the MEDIAMAN_DELETE_ROOTS / /media / /data allow-list are refused."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.get("/api/settings/disk-usage?path=/nonexistent")
        assert resp.status_code == 403
        assert "error" in resp.json()

    def test_returns_error_for_nonexistent_allowlisted_path(self, conn, secret_key, monkeypatch):
        """When the path IS in the allow-list but stat fails, response has an error key."""
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/nonexistent-but-allowed")
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        with patch(
            "mediaman.web.routes.settings_routes.get_disk_usage",
            side_effect=FileNotFoundError("not found"),
        ):
            resp = client.get("/api/settings/disk-usage?path=/nonexistent-but-allowed")

        assert resp.status_code == 200
        assert "error" in resp.json()

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

        assert resp.status_code == 400

    def test_allows_lan_address(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.put(
            "/api/settings",
            json={"radarr_url": "http://192.168.1.50:7878"},
        )

        assert resp.status_code == 200
