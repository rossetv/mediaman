"""Tests for :mod:`mediaman.web.routes.download.status`.

Covers GET /api/download/status:
  - unauthenticated (no token, no admin) → 401
  - authenticated via valid download token
  - authenticated via valid poll token
  - admin session bypasses token requirement
  - invalid/overlong tokens → 401
  - unknown service → unknown state item
  - _format_timeleft helper
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.crypto import generate_download_token, generate_poll_token
from mediaman.db import init_db, set_connection
from mediaman.web.routes.download.status import (
    _DOWNLOAD_STATUS_LIMITER,
    _format_timeleft,
)
from mediaman.web.routes.download.status import (
    router as status_router,
)


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(status_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


def _download_token(secret_key: str, media_type: str = "movie", tmdb_id: int = 42) -> str:
    return generate_download_token(
        email="test@example.com",
        action="download",
        title="Dune",
        media_type=media_type,
        tmdb_id=tmdb_id,
        recommendation_id=None,
        secret_key=secret_key,
    )


class TestFormatTimeleft:
    """Unit tests for the _format_timeleft helper."""

    def test_empty_string_returns_empty(self):
        assert _format_timeleft("") == ""

    def test_none_like_empty_returns_empty(self):
        assert _format_timeleft("") == ""

    def test_invalid_format_returns_empty(self):
        assert _format_timeleft("not-a-time") == ""

    def test_hours_and_minutes(self):
        result = _format_timeleft("02:15:00")
        assert "2 hr" in result
        assert "15 min" in result

    def test_minutes_only(self):
        result = _format_timeleft("00:07:30")
        assert "7 min" in result
        assert "hr" not in result

    def test_seconds_only_returns_at_least_1_sec(self):
        result = _format_timeleft("00:00:45")
        assert "sec" in result
        assert "45" in result

    def test_zero_seconds_returns_1_sec(self):
        """Avoids "~0 sec remaining" — minimum is 1 sec."""
        result = _format_timeleft("00:00:00")
        assert "1 sec" in result

    def test_wrong_part_count_returns_empty(self):
        assert _format_timeleft("10:30") == ""

    def test_non_numeric_parts_returns_empty(self):
        assert _format_timeleft("aa:bb:cc") == ""


class TestDownloadStatusAuth:
    def setup_method(self):
        _DOWNLOAD_STATUS_LIMITER._attempts.clear()

    def test_unauthenticated_returns_401(self, db_path, secret_key):
        """No token, no admin session → 401."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/download/status?service=radarr&tmdb_id=42")
        assert resp.status_code == 401

    def test_valid_download_token_authenticated(self, db_path, secret_key):
        """A valid download token matching the requested tmdb_id is accepted."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _download_token(secret_key, media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get(
                f"/api/download/status?service=radarr&tmdb_id=42&token={token}",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data

    def test_download_token_wrong_tmdb_id_rejected(self, db_path, secret_key):
        """A token for tmdb_id=99 cannot authenticate a status poll for tmdb_id=42."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _download_token(secret_key, media_type="movie", tmdb_id=99)  # wrong ID

        resp = client.get(
            f"/api/download/status?service=radarr&tmdb_id=42&token={token}",
        )
        assert resp.status_code == 401

    def test_valid_poll_token_authenticated(self, db_path, secret_key):
        """A valid poll token for the correct service and tmdb_id is accepted."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        poll_token = generate_poll_token(
            media_item_id="radarr:Dune",
            service="radarr",
            tmdb_id=42,
            secret_key=secret_key,
        )

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get(
                f"/api/download/status?service=radarr&tmdb_id=42&poll_token={poll_token}",
            )
        assert resp.status_code == 200

    def test_admin_session_bypasses_token_requirement(self, db_path, secret_key):
        """An authenticated admin can poll status without any download/poll token."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")
        assert resp.status_code == 200

    def test_overlong_token_returns_401(self, db_path, secret_key):
        """A token over 4096 chars returns 401 without attempting validation."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        long_token = "x" * 4097
        resp = client.get(
            f"/api/download/status?service=radarr&tmdb_id=42&token={long_token}",
        )
        assert resp.status_code == 401

    def test_unknown_service_returns_unknown_item(self, db_path, secret_key):
        """An unrecognised service name returns an 'unknown' state item."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.get("/api/download/status?service=bogus&tmdb_id=42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "unknown"


class TestDownloadStatusRadarr:
    def setup_method(self):
        _DOWNLOAD_STATUS_LIMITER._attempts.clear()

    def test_radarr_movie_with_file_returns_ready(self, db_path, secret_key):
        """When Radarr reports hasFile=True the status is ready."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = {
            "hasFile": True,
            "title": "Dune",
            "images": [],
        }
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ready"
        assert data["progress"] == 100

    def test_radarr_not_configured_returns_unknown(self, db_path, secret_key):
        """When Radarr is not configured (client=None) the status is unknown."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        with patch("mediaman.web.routes.download.status.build_radarr_from_db", return_value=None):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")

        assert resp.status_code == 200
        assert resp.json()["state"] == "unknown"
