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

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import generate_download_token, generate_poll_token
from mediaman.web.routes.download.status import (
    _DOWNLOAD_STATUS_LIMITER,
    _format_timeleft,
)
from mediaman.web.routes.download.status import (
    router as status_router,
)


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
    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _DOWNLOAD_STATUS_LIMITER._attempts.clear()

    def test_unauthenticated_returns_401(self, app_factory, conn):
        """No token, no admin session → 401."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/download/status?service=radarr&tmdb_id=42")
        assert resp.status_code == 401

    def test_download_token_no_longer_accepted(self, app_factory, conn, secret_key):
        """Finding 14: download tokens are no longer accepted for status polling;
        only poll_token (short-lived) is valid for unauthenticated callers."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        token = _download_token(secret_key, media_type="movie", tmdb_id=42)

        # Download token passed as `token=` query param → 401 (param no longer accepted)
        resp = client.get(
            f"/api/download/status?service=radarr&tmdb_id=42&token={token}",
        )
        assert resp.status_code == 401

    def test_download_token_wrong_tmdb_id_rejected(self, app_factory, conn, secret_key):
        """A download token (now unsupported) still returns 401 regardless of tmdb_id match."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        token = _download_token(secret_key, media_type="movie", tmdb_id=99)  # wrong ID

        resp = client.get(
            f"/api/download/status?service=radarr&tmdb_id=42&token={token}",
        )
        assert resp.status_code == 401

    def test_valid_poll_token_authenticated(self, app_factory, conn, secret_key):
        """A valid poll token for the correct service and tmdb_id is accepted."""
        app = app_factory(status_router, conn=conn)
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

    def test_admin_session_bypasses_token_requirement(self, app_factory, authed_client, conn):
        """An authenticated admin can poll status without any download/poll token."""
        app = app_factory(status_router, conn=conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")
        assert resp.status_code == 200

    def test_overlong_token_returns_401(self, app_factory, conn):
        """A token over 4096 chars returns 401 without attempting validation."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)

        long_token = "x" * 4097
        resp = client.get(
            f"/api/download/status?service=radarr&tmdb_id=42&token={long_token}",
        )
        assert resp.status_code == 401

    def test_unknown_service_returns_422(self, app_factory, authed_client, conn):
        """An unrecognised service name is rejected at the type layer.

        Wave 5-4 tightened the route signature to ``service: Literal["radarr",
        "sonarr"]``, so FastAPI's request-validation now returns 422 for any
        other value rather than reaching the handler and falling through to
        an 'unknown' state.
        """
        app = app_factory(status_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.get("/api/download/status?service=bogus&tmdb_id=42")
        assert resp.status_code == 422


class TestDownloadStatusRadarr:
    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _DOWNLOAD_STATUS_LIMITER._attempts.clear()

    def test_radarr_movie_with_file_returns_ready(self, app_factory, authed_client, conn):
        """When Radarr reports hasFile=True the status is ready."""
        app = app_factory(status_router, conn=conn)
        client = authed_client(app, conn)

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

    def test_radarr_not_configured_returns_unknown(self, app_factory, authed_client, conn):
        """When Radarr is not configured (client=None) the status is unknown."""
        app = app_factory(status_router, conn=conn)
        client = authed_client(app, conn)

        with patch("mediaman.web.routes.download.status.build_radarr_from_db", return_value=None):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")

        assert resp.status_code == 200
        assert resp.json()["state"] == "unknown"


# ---------------------------------------------------------------------------
# Status polling endpoints require a poll_token, not the original download token
# ---------------------------------------------------------------------------

_STATUS_SECRET = "a" * 64


def _make_status_app_finding14(conn) -> TestClient:
    """Stand up a status-router app with get_optional_admin always returning None."""

    from fastapi import FastAPI

    from mediaman.config import Config
    from mediaman.db import set_connection
    from mediaman.web.auth.middleware import get_optional_admin
    from mediaman.web.routes.download import status as status_mod

    app = FastAPI()
    app.include_router(status_mod.router)
    app.state.config = Config(secret_key=_STATUS_SECRET)
    app.state.db = conn
    set_connection(conn)
    app.dependency_overrides[get_optional_admin] = lambda: None
    return TestClient(app, raise_server_exceptions=True)


class TestFinding14PollTokenRequired:
    """Finding 14: unauthenticated status polling must use poll_token, not download token."""

    def test_no_token_returns_401(self, conn):
        """Calling /api/download/status without any token must return 401."""
        client = _make_status_app_finding14(conn)
        resp = client.get("/api/download/status", params={"service": "radarr", "tmdb_id": 42})
        assert resp.status_code == 401

    def test_download_token_no_longer_accepted(self, conn):
        """The long-lived download token must not be accepted for polling (finding 14)."""
        download_token = generate_download_token(
            email="test@example.com",
            action="download",
            title="Test",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=None,
            secret_key=_STATUS_SECRET,
        )
        client = _make_status_app_finding14(conn)
        # Passing the download token in the 'token' param must now be ignored
        # (the 'token' param was removed; the endpoint only knows poll_token).
        resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42, "token": download_token},
        )
        # Must not authenticate — 401 expected
        assert resp.status_code == 401

    def test_valid_poll_token_accepted(self, conn):
        """A valid poll_token bound to the correct service/tmdb must authenticate."""
        from mediaman.web.routes.download import status as status_mod

        poll_token = generate_poll_token(
            media_item_id="radarr:Test",
            service="radarr",
            tmdb_id=42,
            secret_key=_STATUS_SECRET,
        )
        client = _make_status_app_finding14(conn)

        # Patch the service lookup so we don't need a real Radarr client
        with patch.object(
            status_mod,
            "_radarr_status",
            return_value={"state": "searching"},
        ):
            resp = client.get(
                "/api/download/status",
                params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
            )
        assert resp.status_code == 200

    def test_poll_token_wrong_service_rejected(self, conn):
        """A poll_token issued for 'sonarr' must not authenticate a 'radarr' request."""
        poll_token = generate_poll_token(
            media_item_id="sonarr:Test",
            service="sonarr",
            tmdb_id=42,
            secret_key=_STATUS_SECRET,
        )
        client = _make_status_app_finding14(conn)
        resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
        )
        assert resp.status_code == 401
