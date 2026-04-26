"""Tests for :mod:`mediaman.web.routes.download.submit`.

Covers POST /download/{token}:
  - invalid/expired token → 410
  - overlong token → 410
  - token replay (second use) → 409
  - movie media type → Radarr add, poll token returned
  - tv media type → Sonarr add, poll token returned
  - Radarr not configured → 503
  - Sonarr not configured → 503
  - Arr HTTP 409 (already exists) → 409 with poll token
  - generic Arr failure → 502, token unmarked for retry
  - DB notification record written on success
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import generate_download_token, validate_poll_token
from mediaman.db import init_db, set_connection
from mediaman.services.infra.http_client import SafeHTTPError
from mediaman.web.routes.download._tokens import _USED_TOKENS, _USED_TOKENS_LOCK
from mediaman.web.routes.download.submit import (
    _DOWNLOAD_LIMITER_POST,
)
from mediaman.web.routes.download.submit import (
    router as submit_router,
)


def _clear_used_tokens():
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.clear()


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(submit_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _make_token(
    secret_key: str,
    title: str = "Dune",
    media_type: str = "movie",
    tmdb_id: int | None = 42,
) -> str:
    return generate_download_token(
        email="viewer@example.com",
        action="download",
        title=title,
        media_type=media_type,
        tmdb_id=tmdb_id,
        recommendation_id=None,
        secret_key=secret_key,
    )


class TestDownloadSubmitValidation:
    def setup_method(self):
        _clear_used_tokens()
        _DOWNLOAD_LIMITER_POST._attempts.clear()

    def test_invalid_token_returns_410(self, db_path, secret_key):
        """A tampered/invalid token is rejected with 410."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/download/this-is-garbage")
        assert resp.status_code == 410
        assert resp.json()["ok"] is False

    def test_overlong_token_returns_410(self, db_path, secret_key):
        """A token over 4096 chars is rejected with 410 without signature check."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post(f"/download/{'x' * 4097}")
        assert resp.status_code == 410

    def test_token_replay_returns_409(self, db_path, secret_key):
        """Using the same token a second time returns 409."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            first = client.post(f"/download/{token}")
            second = client.post(f"/download/{token}")

        assert first.status_code == 200
        assert second.status_code == 409
        assert "already been used" in second.json()["error"]


class TestDownloadSubmitMovie:
    def setup_method(self):
        _clear_used_tokens()
        _DOWNLOAD_LIMITER_POST._attempts.clear()

    def test_movie_token_calls_radarr_add_movie(self, db_path, secret_key):
        """A movie token results in Radarr.add_movie being called."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["service"] == "radarr"
        mock_radarr.add_movie.assert_called_once_with(42, "Dune")

    def test_movie_response_includes_poll_token(self, db_path, secret_key):
        """Successful movie submit returns a valid poll token."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        poll_token = resp.json().get("poll_token")
        assert poll_token is not None
        poll_payload = validate_poll_token(poll_token, secret_key)
        assert poll_payload is not None
        assert poll_payload.get("svc") == "radarr" and poll_payload.get("tmdb") == 42

    def test_radarr_not_configured_returns_503(self, db_path, secret_key):
        """When Radarr is not configured, submit returns 503 and token is unmarked."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, media_type="movie", tmdb_id=42)

        with patch("mediaman.web.routes.download.submit.build_radarr_from_db", return_value=None):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 503
        assert resp.json()["ok"] is False
        # Token must be unmarked so the user can retry after configuring Radarr
        with _USED_TOKENS_LOCK:
            import hashlib

            digest = hashlib.sha256(token.encode()).hexdigest()
            assert digest not in _USED_TOKENS

    def test_radarr_409_safe_http_error_returns_conflict_with_poll_token(self, db_path, secret_key):
        """If Radarr raises SafeHTTPError 409 (already exists), returns 409 with poll token."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://radarr/api/v3/movie"
        )

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 409
        body = resp.json()
        assert body["ok"] is False
        assert "already exists" in body["error"]
        assert "poll_token" in body

    def test_radarr_502_returns_error_and_token_unmarked(self, db_path, secret_key):
        """A non-409 Arr error returns 502 and the token is unmarked for retry."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.side_effect = SafeHTTPError(
            status_code=500, body_snippet="internal error", url="http://radarr/api/v3/movie"
        )

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 502
        # Token unmarked — user can retry
        import hashlib

        digest = hashlib.sha256(token.encode()).hexdigest()
        with _USED_TOKENS_LOCK:
            assert digest not in _USED_TOKENS

    def test_notification_recorded_on_success(self, db_path, secret_key):
        """A successful submit records a download notification in the DB."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        row = conn.execute(
            "SELECT * FROM download_notifications WHERE title='Dune' AND media_type='movie'"
        ).fetchone()
        assert row is not None


class TestDownloadSubmitTV:
    def setup_method(self):
        _clear_used_tokens()
        _DOWNLOAD_LIMITER_POST._attempts.clear()

    def test_tv_token_calls_sonarr_add_series(self, db_path, secret_key):
        """A TV token results in Sonarr.add_series being called."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, title="Severance", media_type="tv", tmdb_id=136315)

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        mock_sonarr.add_series.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_sonarr_from_db", return_value=mock_sonarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["service"] == "sonarr"
        mock_sonarr.add_series.assert_called_once_with(999, "Severance")

    def test_sonarr_no_tvdb_id_returns_422(self, db_path, secret_key):
        """If the Sonarr lookup result has no tvdbId, submit returns 422 and unmarks token."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, title="Mystery Show", media_type="tv", tmdb_id=999)

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": None}]

        with patch(
            "mediaman.web.routes.download.submit.build_sonarr_from_db", return_value=mock_sonarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 422
        assert resp.json()["ok"] is False

    def test_sonarr_not_configured_returns_503(self, db_path, secret_key):
        """When Sonarr is not configured, TV submit returns 503."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_token(secret_key, media_type="tv", tmdb_id=42)

        with patch("mediaman.web.routes.download.submit.build_sonarr_from_db", return_value=None):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 503
