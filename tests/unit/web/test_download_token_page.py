"""Tests for GET/POST /download/{token} — the token-gated download confirmation page."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.crypto import generate_download_token
from mediaman.db import init_db, set_connection
from mediaman.web.routes.download import router as download_router
from mediaman.web.routes import download as download_mod


def _make_app(conn, secret_key: str) -> FastAPI:
    """Build a minimal FastAPI app wired to *conn* for testing."""
    app = FastAPI()
    app.include_router(download_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)

    # Mock the templates so TemplateResponse calls are inspectable
    mock_templates = MagicMock()

    def fake_template_response(request, template_name, ctx):
        return HTMLResponse(json.dumps(ctx), status_code=200)

    mock_templates.TemplateResponse.side_effect = fake_template_response
    app.state.templates = mock_templates
    return app


def _valid_token(secret_key: str, title: str = "Dune", media_type: str = "movie", tmdb_id: int = 42) -> str:
    return generate_download_token(
        email="test@example.com",
        action="download",
        title=title,
        media_type=media_type,
        tmdb_id=tmdb_id,
        recommendation_id=None,
        secret_key=secret_key,
    )


class TestDownloadPageGet:
    def setup_method(self):
        """Clear in-memory state between tests."""
        download_mod._USED_TOKENS.clear()
        download_mod._DOWNLOAD_LIMITER_GET._attempts.clear()
        download_mod._DOWNLOAD_LIMITER_POST._attempts.clear()

    def test_valid_token_renders_confirm_state(self, db_path, secret_key):
        """GET with a valid token returns state=confirm with item details."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with patch("mediaman.web.routes.download._build_radarr", return_value=mock_radarr):
            resp = client.get(f"/download/{token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "confirm"
        assert ctx["item"] is not None
        assert ctx["item"]["title"] == "Dune"

    def test_invalid_token_renders_expired_state(self, db_path, secret_key):
        """GET with an invalid/tampered token returns state=expired."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        resp = client.get("/download/this.is.not.a.valid.token")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "expired"
        assert ctx["item"] is None

    def test_overlong_token_renders_expired_state(self, db_path, secret_key):
        """GET with a token over 4096 chars returns state=expired without decoding."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        long_token = "x" * 4097
        resp = client.get(f"/download/{long_token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "expired"

    def test_valid_token_for_movie_already_in_library(self, db_path, secret_key):
        """When Radarr says hasFile=True, download_state is in_library."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = {"hasFile": True, "title": "Dune"}

        with patch("mediaman.web.routes.download._build_radarr", return_value=mock_radarr):
            resp = client.get(f"/download/{token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "confirm"
        assert ctx["item"]["download_state"] == "in_library"

    def test_rate_limiter_blocks_excess_get_requests(self, db_path, secret_key):
        """GET rate limiter fires after max_attempts is exceeded."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        download_mod._DOWNLOAD_LIMITER_GET._attempts.clear()
        cap = download_mod._DOWNLOAD_LIMITER_GET._max_attempts

        token = _valid_token(secret_key)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        try:
            with patch("mediaman.web.routes.download._build_radarr", return_value=mock_radarr):
                for _ in range(cap):
                    r = client.get(f"/download/{token}")
                    assert r.status_code == 200

                r = client.get(f"/download/{token}")
            assert r.status_code == 429
        finally:
            download_mod._DOWNLOAD_LIMITER_GET._attempts.clear()


class TestDownloadPagePost:
    def setup_method(self):
        """Clear in-memory state between tests."""
        download_mod._USED_TOKENS.clear()
        download_mod._DOWNLOAD_LIMITER_GET._attempts.clear()
        download_mod._DOWNLOAD_LIMITER_POST._attempts.clear()

    def test_post_valid_movie_token_calls_radarr(self, db_path, secret_key):
        """POST with a valid movie token triggers Radarr add_movie."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch("mediaman.web.routes.download._build_radarr", return_value=mock_radarr):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["service"] == "radarr"
        mock_radarr.add_movie.assert_called_once_with(42, "Dune")

    def test_post_valid_tv_token_calls_sonarr(self, db_path, secret_key):
        """POST with a valid TV token triggers Sonarr add_series."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key, title="Severance", media_type="tv", tmdb_id=99)

        mock_sonarr = MagicMock()
        mock_sonarr._get.return_value = [{"tvdbId": 12345, "tmdbId": 99}]
        mock_sonarr.add_series.return_value = None

        with patch("mediaman.web.routes.download._build_sonarr", return_value=mock_sonarr):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["service"] == "sonarr"
        mock_sonarr.add_series.assert_called_once()

    def test_post_with_already_used_token_returns_409(self, db_path, secret_key):
        """POST with a token that's already been used returns HTTP 409."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch("mediaman.web.routes.download._build_radarr", return_value=mock_radarr):
            # First POST — consumes the token
            resp1 = client.post(f"/download/{token}")
            assert resp1.status_code == 200
            assert resp1.json()["ok"] is True

            # Second POST — must be rejected
            resp2 = client.post(f"/download/{token}")

        assert resp2.status_code == 409
        assert resp2.json()["ok"] is False

    def test_post_invalid_token_returns_410(self, db_path, secret_key):
        """POST with an invalid token returns HTTP 410."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        resp = client.post("/download/not.a.real.token")

        assert resp.status_code == 410
        data = resp.json()
        assert data["ok"] is False

    def test_post_rate_limiter_blocks_excess_requests(self, db_path, secret_key):
        """POST rate limiter fires after max_attempts is exceeded."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        download_mod._DOWNLOAD_LIMITER_POST._attempts.clear()
        cap = download_mod._DOWNLOAD_LIMITER_POST._max_attempts

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        try:
            with patch("mediaman.web.routes.download._build_radarr", return_value=mock_radarr):
                # Burn through the window using distinct tokens so none are "already used"
                for i in range(cap):
                    t = generate_download_token(
                        email=f"u{i}@example.com",
                        action="download",
                        title=f"Movie {i}",
                        media_type="movie",
                        tmdb_id=i + 1,
                        recommendation_id=None,
                        secret_key=secret_key,
                    )
                    r = client.post(f"/download/{t}")
                    assert r.status_code == 200

                # Next call must be rate-limited
                t_extra = generate_download_token(
                    email="extra@example.com",
                    action="download",
                    title="Extra Movie",
                    media_type="movie",
                    tmdb_id=9999,
                    recommendation_id=None,
                    secret_key=secret_key,
                )
                r = client.post(f"/download/{t_extra}")

            assert r.status_code == 429
        finally:
            download_mod._DOWNLOAD_LIMITER_POST._attempts.clear()
            download_mod._USED_TOKENS.clear()
