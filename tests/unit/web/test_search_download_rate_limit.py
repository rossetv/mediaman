"""Tests for POST /api/search/download rate limiter and dedup (finding 33)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.web.routes.search import (
    _DOWNLOAD_ADMIN_LIMITER,
    _DOWNLOAD_IP_LIMITER,
    _download_dedup,
)


@pytest.fixture
def app(db_path, secret_key):
    conn = init_db(str(db_path))
    set_connection(conn)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
        "VALUES ('tmdb_read_token', 'test-token', 0, datetime('now'))"
    )
    conn.commit()
    application = create_app()
    application.state.config = MagicMock(secret_key=secret_key, data_dir=str(db_path.parent))
    application.state.db = conn
    yield application
    conn.close()


@pytest.fixture
def authed_client(app):
    from mediaman.web.auth.session import create_session, create_user

    create_user(app.state.db, "admin", "password123", enforce_policy=False)
    token = create_session(app.state.db, "admin")
    client = TestClient(app)
    client.cookies.set("session_token", token)
    # CSRF middleware (finding 11) refuses cookie-authenticated unsafe
    # requests with no Origin/Referer; supply a same-origin Origin so
    # the rate-limit test reaches the limiter rather than the CSRF gate.
    client.headers.update({"Origin": "http://testserver"})
    return client


def _valid_body(media_type: str = "movie", tmdb_id: int = 42):
    return {"media_type": media_type, "tmdb_id": tmdb_id, "title": "Dune"}


def _setup_limiters():
    _DOWNLOAD_ADMIN_LIMITER.reset()
    _DOWNLOAD_IP_LIMITER.reset()
    _download_dedup.clear()


class TestSearchDownloadRateLimit:
    """POST /api/search/download must enforce per-admin and per-IP limits."""

    def setup_method(self):
        _setup_limiters()

    def test_normal_request_succeeds(self, authed_client):
        """A single well-formed request is not rate-limited."""
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        with patch(
            "mediaman.web.routes.search.download.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = authed_client.post("/api/search/download", json=_valid_body())
        assert resp.status_code != 429

    def test_eleventh_admin_request_in_window_returns_429(self, authed_client):
        """11th request within the per-admin burst window returns 429."""
        cap = _DOWNLOAD_ADMIN_LIMITER._max_in_window

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with patch(
            "mediaman.web.routes.search.download.build_radarr_from_db", return_value=mock_radarr
        ):
            for i in range(cap):
                _download_dedup.clear()  # prevent dedup firing
                resp = authed_client.post(
                    "/api/search/download",
                    json=_valid_body(tmdb_id=1000 + i),
                )
                assert resp.status_code != 429, f"Rate limit fired too early (iteration {i})"

        _download_dedup.clear()
        resp = authed_client.post(
            "/api/search/download",
            json=_valid_body(tmdb_id=9999),
        )
        assert resp.status_code == 429
        assert resp.json()["ok"] is False

    def test_unauthenticated_request_returns_401(self, app):
        """Unauthenticated requests are rejected before the rate limiter fires."""
        client = TestClient(app)
        resp = client.post("/api/search/download", json=_valid_body())
        assert resp.status_code in (401, 302, 303)


class TestSearchDownloadDedup:
    """Duplicate (username, tmdb_id, media_type) requests are suppressed."""

    def setup_method(self):
        _setup_limiters()

    def test_duplicate_request_returns_429(self, authed_client):
        """Two identical requests within the dedup window: second gets 429."""
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with patch(
            "mediaman.web.routes.search.download.build_radarr_from_db", return_value=mock_radarr
        ):
            first = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=99))
        # First request may succeed or fail (Radarr mock) but must not be 429.
        assert first.status_code != 429

        with patch(
            "mediaman.web.routes.search.download.build_radarr_from_db", return_value=mock_radarr
        ):
            second = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=99))
        assert second.status_code == 429
        assert second.json()["ok"] is False

    def test_different_tmdb_id_not_suppressed(self, authed_client):
        """Different tmdb_ids are not treated as duplicates."""
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with patch(
            "mediaman.web.routes.search.download.build_radarr_from_db", return_value=mock_radarr
        ):
            first = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=11))
            second = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=22))

        assert first.status_code != 429
        assert second.status_code != 429
