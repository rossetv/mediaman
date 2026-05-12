"""Tests for :mod:`mediaman.web.routes.download.confirm`.

Covers the GET /download/{token} confirmation page:
  - valid token → confirm state
  - invalid/expired token → expired state
  - overlong token → expired state (no expensive validation)
  - suggestions DB row enriches the item
  - validate_youtube_id helper behaviour
  - rate-limit guard
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.crypto import generate_download_token
from mediaman.web.routes.download.confirm import (
    _DOWNLOAD_LIMITER_GET,
    _reset_arr_cache_for_tests,
    validate_youtube_id,
)
from mediaman.web.routes.download.confirm import (
    router as confirm_router,
)


def _make_app(app_factory, conn):
    """Build the confirm app + a JSON-echoing templates stub."""
    mock_templates = MagicMock()

    def fake_template_response(request, template_name, ctx):
        return HTMLResponse(json.dumps(ctx), status_code=200)

    mock_templates.TemplateResponse.side_effect = fake_template_response
    return app_factory(confirm_router, conn=conn, state_extras={"templates": mock_templates})


def _valid_token(
    secret_key: str,
    title: str = "Dune",
    media_type: str = "movie",
    tmdb_id: int | None = 42,
    recommendation_id: int | None = None,
) -> str:
    return generate_download_token(
        email="user@example.com",
        action="download",
        title=title,
        media_type=media_type,
        tmdb_id=tmdb_id,
        recommendation_id=recommendation_id,
        secret_key=secret_key,
    )


class TestValidateYoutubeId:
    """Direct unit tests for the validate_youtube_id helper."""

    def test_valid_11_char_id_returned(self):
        """A valid 11-character URL-safe base64 ID is returned unchanged."""
        assert validate_youtube_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_none_input_returns_none(self):
        assert validate_youtube_id(None) is None

    def test_empty_string_returns_none(self):
        assert validate_youtube_id("") is None

    def test_too_short_returns_none(self):
        assert validate_youtube_id("abc") is None

    def test_too_long_returns_none(self):
        assert validate_youtube_id("dQw4w9WgXcQextra") is None

    def test_invalid_chars_returns_none(self):
        """IDs with characters outside [A-Za-z0-9_-] are rejected."""
        assert validate_youtube_id("dQw4w9WgX!@") is None

    def test_underscore_and_hyphen_allowed(self):
        """Underscores and hyphens are valid URL-safe base64 characters."""
        # Exactly 11 chars with underscore and hyphen
        assert validate_youtube_id("abc_def-ghi") == "abc_def-ghi"


_CONFIRM_MODULE = "mediaman.web.routes.download.confirm"


class TestDownloadPageConfirm:
    @pytest.fixture(autouse=True)
    def _arr_stubs(self):
        """Patch Arr infrastructure boundaries and reset per-process caches.

        Every test in this class exercises the confirm page at the HTTP layer;
        none of them care about Radarr/Sonarr state.  Patching the two
        TTL-cached helpers (rather than their four internal callees) keeps
        each test body free of infrastructure mocks.
        """
        _reset_arr_cache_for_tests()
        _DOWNLOAD_LIMITER_GET._attempts.clear()
        with (
            patch(f"{_CONFIRM_MODULE}._get_radarr_cache_cached", return_value={}),
            patch(f"{_CONFIRM_MODULE}._get_sonarr_cache_cached", return_value={}),
            patch(f"{_CONFIRM_MODULE}.compute_download_state", return_value=None),
        ):
            yield
        _reset_arr_cache_for_tests()

    def test_valid_token_returns_confirm_state(self, app_factory, conn, secret_key):
        """GET with a valid token renders the confirm state."""
        app = _make_app(app_factory, conn)
        client = TestClient(app)
        token = _valid_token(secret_key)

        resp = client.get(f"/download/{token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "confirm"
        assert ctx["item"]["title"] == "Dune"

    def test_invalid_token_renders_expired_state(self, app_factory, conn):
        """GET with a tampered/invalid token renders the expired state."""
        app = _make_app(app_factory, conn)
        client = TestClient(app)

        resp = client.get("/download/this-is-not-a-valid-token")
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "expired"
        assert ctx["item"] is None

    def test_overlong_token_renders_expired_state(self, app_factory, conn):
        """Tokens over 4096 chars are rejected before signature validation."""
        app = _make_app(app_factory, conn)
        client = TestClient(app)

        resp = client.get(f"/download/{'x' * 4097}")
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "expired"

    def test_genres_json_is_parsed_into_list(self, app_factory, conn, secret_key):
        """Genres stored as JSON string are expanded into genres_list."""
        # Insert a suggestion row with genres
        conn.execute(
            "INSERT INTO suggestions (id, title, media_type, created_at, "
            "poster_url, year, description, reason, rating, rt_rating, tagline, runtime, "
            "genres, cast_json, director, trailer_key, imdb_rating, metascore) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "Dune",
                "movie",
                "2024-01-01",
                None,
                2021,
                None,
                None,
                None,
                None,
                None,
                None,
                '["Sci-Fi","Drama"]',
                None,
                None,
                None,
                None,
                None,
            ),
        )
        conn.commit()

        token = generate_download_token(
            email="user@example.com",
            action="download",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=1,  # sid=1 → look up suggestions
            secret_key=secret_key,
        )

        app = _make_app(app_factory, conn)
        client = TestClient(app)
        resp = client.get(f"/download/{token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "confirm"
        assert "Sci-Fi" in ctx["item"]["genres_list"]
        assert "Drama" in ctx["item"]["genres_list"]
