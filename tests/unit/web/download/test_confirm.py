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

from unittest.mock import patch

import pytest
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
from tests.helpers.factories import insert_suggestion


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

    def test_valid_token_returns_confirm_state(self, app_factory, conn, secret_key, templates_stub):
        """GET with a valid token renders the confirm state."""
        app = app_factory(confirm_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)
        token = _valid_token(secret_key)

        resp = client.get(f"/download/{token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "confirm"
        assert ctx["item"]["title"] == "Dune"

    def test_invalid_token_renders_expired_state(self, app_factory, conn, templates_stub):
        """GET with a tampered/invalid token renders the expired state."""
        app = app_factory(confirm_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        resp = client.get("/download/this-is-not-a-valid-token")
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "expired"
        assert ctx["item"] is None

    def test_overlong_token_renders_expired_state(self, app_factory, conn, templates_stub):
        """Tokens over 4096 chars are rejected before signature validation."""
        app = app_factory(confirm_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        resp = client.get(f"/download/{'x' * 4097}")
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "expired"

    def test_genres_json_is_parsed_into_list(self, app_factory, conn, secret_key, templates_stub):
        """Genres stored as JSON string are expanded into genres_list."""
        # Insert a suggestion row with genres
        rec_id = insert_suggestion(
            conn,
            title="Dune",
            media_type="movie",
            year=2021,
            genres='["Sci-Fi","Drama"]',
            created_at="2024-01-01",
        )

        token = generate_download_token(
            email="user@example.com",
            action="download",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=rec_id,  # look up suggestions by factory-assigned id
            secret_key=secret_key,
        )

        app = app_factory(confirm_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)
        resp = client.get(f"/download/{token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "confirm"
        assert "Sci-Fi" in ctx["item"]["genres_list"]


# ---------------------------------------------------------------------------
# Finding 19: Trailer key validation
# ---------------------------------------------------------------------------


class TestFinding19TrailerKeyValidation:
    """Finding 19: trailer key must be exactly 11 URL-safe base64 characters."""

    def _validate(self, key: str) -> bool:
        return validate_youtube_id(key) is not None

    def test_valid_11_char_key_accepted(self):
        assert self._validate("dQw4w9WgXcQ")

    def test_10_char_key_rejected(self):
        assert not self._validate("dQw4w9WgXc")

    def test_12_char_key_rejected(self):
        assert not self._validate("dQw4w9WgXcQQ")

    def test_key_with_invalid_chars_rejected(self):
        assert not self._validate("dQw4w9WgX!Q")

    def test_none_returns_none(self):
        assert validate_youtube_id(None) is None

    def test_empty_string_returns_none(self):
        assert validate_youtube_id("") is None
