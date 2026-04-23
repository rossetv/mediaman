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
from mediaman.crypto import generate_download_token, validate_poll_token
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

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
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
        mock_radarr.get_movies.return_value = [
            {"tmdbId": 42, "hasFile": True, "title": "Dune"}
        ]
        mock_radarr.get_queue.return_value = []

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
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
            with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
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

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
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
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 12345, "tmdbId": 99}]
        mock_sonarr.add_series.return_value = None

        with patch("mediaman.web.routes.download.build_sonarr_from_db", return_value=mock_sonarr):
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

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
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
            with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
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


class TestTwoPhaseConsumption:
    """C14 — token must be released back on transient Radarr/Sonarr errors."""

    def setup_method(self):
        download_mod._USED_TOKENS.clear()
        download_mod._DOWNLOAD_LIMITER_GET._attempts.clear()
        download_mod._DOWNLOAD_LIMITER_POST._attempts.clear()

    def test_token_released_on_radarr_exception(self, db_path, secret_key):
        """A transient exception from Radarr must not permanently burn the token."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.side_effect = Exception("Connection refused")

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
            resp1 = client.post(f"/download/{token}")

        # The call failed — should not be 200 ok
        assert resp1.json()["ok"] is False

        # Token must have been released; a second attempt must not return 409
        mock_radarr2 = MagicMock()
        mock_radarr2.add_movie.return_value = None

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr2):
            resp2 = client.post(f"/download/{token}")

        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True

    def test_token_released_on_sonarr_exception(self, db_path, secret_key):
        """A transient exception from Sonarr must not permanently burn the token."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key, title="Severance", media_type="tv", tmdb_id=99)

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.side_effect = Exception("Timeout")

        with patch("mediaman.web.routes.download.build_sonarr_from_db", return_value=mock_sonarr):
            resp1 = client.post(f"/download/{token}")

        assert resp1.json()["ok"] is False

        # Should be retryable
        mock_sonarr2 = MagicMock()
        mock_sonarr2.lookup_by_tmdb_id.return_value = [{"tvdbId": 777, "tmdbId": 99}]
        mock_sonarr2.add_series.return_value = None

        with patch("mediaman.web.routes.download.build_sonarr_from_db", return_value=mock_sonarr2):
            resp2 = client.post(f"/download/{token}")

        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True

    def test_token_not_released_on_409(self, db_path, secret_key):
        """An HTTP 409 from Radarr means the item already exists — token stays consumed."""
        import requests as req_lib

        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_radarr = MagicMock()
        mock_radarr.add_movie.side_effect = req_lib.HTTPError(response=mock_resp)

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
            resp1 = client.post(f"/download/{token}")

        assert resp1.status_code == 200  # our handler returns 200 for 409 from Arr
        assert resp1.json()["ok"] is False
        assert "already exists" in resp1.json()["error"]

        # Second attempt must be blocked — item exists, token is spent
        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
            resp2 = client.post(f"/download/{token}")

        assert resp2.status_code == 409


class TestPollingCapability:
    """C24 — POST /download/{token} issues a poll_token; status endpoint requires it."""

    def setup_method(self):
        download_mod._USED_TOKENS.clear()
        download_mod._DOWNLOAD_LIMITER_GET._attempts.clear()
        download_mod._DOWNLOAD_LIMITER_POST._attempts.clear()

    def test_successful_post_returns_poll_token(self, db_path, secret_key):
        """A successful POST /download/{token} for a movie includes a poll_token."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)
        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "poll_token" in data
        # Validate the poll_token is scoped to the correct service + tmdb_id
        assert validate_poll_token(
            data["poll_token"], secret_key, service="radarr", tmdb_id=42
        )

    def test_poll_token_validates_wrong_service(self, db_path, secret_key):
        """A radarr poll_token must not validate for sonarr."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)
        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
            resp = client.post(f"/download/{token}")

        pt = resp.json()["poll_token"]
        assert not validate_poll_token(pt, secret_key, service="sonarr", tmdb_id=42)
        assert not validate_poll_token(pt, secret_key, service="radarr", tmdb_id=99)

    def test_status_endpoint_accepts_poll_token(self, db_path, secret_key):
        """GET /api/download/status with a valid poll_token returns 200 without admin session."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        # Mint a poll_token via a download
        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)
        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
            post_resp = client.post(f"/download/{token}")

        poll_token = post_resp.json()["poll_token"]

        mock_radarr2 = MagicMock()
        mock_radarr2.get_movie_by_tmdb.return_value = {"hasFile": True, "title": "Dune", "images": []}
        mock_radarr2.get_queue.return_value = []

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr2):
            status_resp = client.get(
                "/api/download/status",
                params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
            )

        assert status_resp.status_code == 200
        assert status_resp.json()["state"] == "ready"

    def test_status_endpoint_rejects_mismatched_poll_token(self, db_path, secret_key):
        """A poll_token for tmdb_id=99 must be rejected for a query on tmdb_id=42."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        # Mint token for tmdb_id=99
        token99 = _valid_token(secret_key, title="Other", media_type="movie", tmdb_id=99)
        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch("mediaman.web.routes.download.build_radarr_from_db", return_value=mock_radarr):
            post_resp = client.post(f"/download/{token99}")

        poll_token_for_99 = post_resp.json()["poll_token"]

        # Try to use it to query tmdb_id=42
        status_resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token_for_99},
        )
        assert status_resp.status_code == 401

    def test_status_endpoint_rejects_no_auth(self, db_path, secret_key):
        """GET /api/download/status with no token of any kind returns 401."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42},
        )
        assert resp.status_code == 401


class TestYoutubeTrailerKeyValidation:
    """H72 — trailer_key must pass the YouTube ID regex before reaching the template."""

    def test_valid_youtube_id_passes_through(self):
        """A well-formed 11-char YouTube ID is returned unchanged."""
        from mediaman.web.routes.download import _validate_youtube_id
        assert _validate_youtube_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_id_is_rejected(self):
        """An ID shorter than 11 chars is rejected."""
        from mediaman.web.routes.download import _validate_youtube_id
        assert _validate_youtube_id("short") is None

    def test_long_id_is_rejected(self):
        """An ID longer than 11 chars is rejected."""
        from mediaman.web.routes.download import _validate_youtube_id
        assert _validate_youtube_id("a" * 12) is None

    def test_id_with_slash_is_rejected(self):
        """An ID containing a slash (path traversal) is rejected."""
        from mediaman.web.routes.download import _validate_youtube_id
        assert _validate_youtube_id("abc/def1234") is None

    def test_id_with_angle_brackets_is_rejected(self):
        """An ID containing HTML metacharacters is rejected."""
        from mediaman.web.routes.download import _validate_youtube_id
        assert _validate_youtube_id("<script>abc") is None

    def test_none_input_returns_none(self):
        """None input returns None without raising."""
        from mediaman.web.routes.download import _validate_youtube_id
        assert _validate_youtube_id(None) is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        from mediaman.web.routes.download import _validate_youtube_id
        assert _validate_youtube_id("") is None

    def test_valid_id_with_dash_and_underscore(self):
        """YouTube IDs may contain - and _ characters."""
        from mediaman.web.routes.download import _validate_youtube_id
        assert _validate_youtube_id("abc-efg_hij") == "abc-efg_hij"

    def test_suggestion_trailer_key_validated_on_page_load(self, db_path, secret_key):
        """A malicious trailer_key from the DB is sanitised to None before rendering."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app)

        # Insert a suggestion with a malicious trailer_key
        conn.execute(
            "INSERT INTO suggestions (title, media_type, category, tmdb_id, trailer_key, created_at) "
            "VALUES ('Dune', 'movie', 'personal', 42, '<script>evil</script>', '2026-01-01')"
        )
        conn.commit()
        row = conn.execute("SELECT id FROM suggestions WHERE title='Dune'").fetchone()
        sid = row["id"]

        from mediaman.crypto import generate_download_token
        token = generate_download_token(
            email="test@example.com",
            action="download",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=sid,
            secret_key=secret_key,
        )

        resp = client.get(f"/download/{token}")
        assert resp.status_code == 200
        ctx = resp.json()
        # The malicious key must have been sanitised
        assert ctx["item"]["trailer_key"] is None
