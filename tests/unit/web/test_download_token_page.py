"""Tests for GET/POST /download/{token} — the token-gated download confirmation page."""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import generate_download_token, validate_poll_token
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.web.routes.download import _tokens as _download_tokens
from mediaman.web.routes.download import confirm as _download_confirm
from mediaman.web.routes.download import router as download_router
from mediaman.web.routes.download import submit as _download_submit
from tests.helpers.factories import insert_admin_user, insert_suggestion, insert_user_pick


def _valid_token(
    secret_key: str, title: str = "Dune", media_type: str = "movie", tmdb_id: int = 42
) -> str:
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
    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_tokens._USED_TOKENS.clear()
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_submit._DOWNLOAD_LIMITER_POST.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_valid_token_renders_confirm_state(self, app_factory, conn, secret_key, templates_stub):
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with patch(
            "mediaman.web.routes.download.confirm.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get(f"/download/{token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "confirm"
        assert ctx["item"] is not None
        assert ctx["item"]["title"] == "Dune"

    def test_invalid_token_renders_expired_state(self, app_factory, conn, templates_stub):
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        resp = client.get("/download/this.is.not.a.valid.token")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "expired"
        assert ctx["item"] is None

    def test_overlong_token_renders_expired_state(self, app_factory, conn, templates_stub):
        """GET with a token over 4096 chars returns state=expired without decoding."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        long_token = "x" * 4097
        resp = client.get(f"/download/{long_token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "expired"

    def test_valid_token_for_movie_already_in_library(
        self, app_factory, conn, secret_key, templates_stub
    ):
        """When Radarr says hasFile=True, download_state is in_library."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.get_movies.return_value = [{"tmdbId": 42, "hasFile": True, "title": "Dune"}]
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.confirm.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get(f"/download/{token}")

        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["state"] == "confirm"
        assert ctx["item"]["download_state"] == "in_library"

    def test_rate_limiter_blocks_excess_get_requests(
        self, app_factory, conn, secret_key, templates_stub
    ):
        """GET rate limiter fires after max_attempts is exceeded."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        cap = _download_confirm._DOWNLOAD_LIMITER_GET._max_attempts

        token = _valid_token(secret_key)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        try:
            with patch(
                "mediaman.web.routes.download.confirm.build_radarr_from_db",
                return_value=mock_radarr,
            ):
                for _ in range(cap):
                    r = client.get(f"/download/{token}")
                    assert r.status_code == 200

                r = client.get(f"/download/{token}")
            assert r.status_code == 429
        finally:
            _download_confirm._DOWNLOAD_LIMITER_GET.reset()


class TestDownloadPagePost:
    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_tokens._USED_TOKENS.clear()
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_submit._DOWNLOAD_LIMITER_POST.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_post_valid_movie_token_calls_radarr(
        self, app_factory, conn, secret_key, templates_stub
    ):
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
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

    def test_post_valid_tv_token_calls_sonarr(self, app_factory, conn, secret_key, templates_stub):
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key, title="Severance", media_type="tv", tmdb_id=99)

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 12345, "tmdbId": 99}]
        mock_sonarr.add_series.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_sonarr_from_db", return_value=mock_sonarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["service"] == "sonarr"
        mock_sonarr.add_series.assert_called_once()

    def test_post_with_already_used_token_returns_409(
        self, app_factory, conn, secret_key, templates_stub
    ):
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            # First POST — consumes the token
            resp1 = client.post(f"/download/{token}")
            assert resp1.status_code == 200
            assert resp1.json()["ok"] is True

            # Second POST — must be rejected
            resp2 = client.post(f"/download/{token}")

        assert resp2.status_code == 409
        assert resp2.json()["ok"] is False

    def test_post_invalid_token_returns_410(self, app_factory, conn, templates_stub):
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        resp = client.post("/download/not.a.real.token")

        assert resp.status_code == 410
        data = resp.json()
        assert data["ok"] is False

    def test_post_rate_limiter_blocks_excess_requests(
        self, app_factory, conn, secret_key, templates_stub
    ):
        """POST rate limiter fires after max_attempts is exceeded."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        _download_submit._DOWNLOAD_LIMITER_POST.reset()
        cap = _download_submit._DOWNLOAD_LIMITER_POST._max_attempts

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        try:
            with patch(
                "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
            ):
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
            _download_submit._DOWNLOAD_LIMITER_POST.reset()
            _download_tokens._USED_TOKENS.clear()


class TestTwoPhaseConsumption:
    """Token must be released back on transient Radarr/Sonarr errors."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_tokens._USED_TOKENS.clear()
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_submit._DOWNLOAD_LIMITER_POST.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_token_released_on_radarr_exception(
        self, app_factory, conn, secret_key, templates_stub
    ):
        import requests

        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        # Use a transport-shaped exception — the submit handler narrows
        # its broad ``except Exception`` to network/DB errors so unrelated
        # control exceptions (CancelledError, KeyboardInterrupt) propagate.
        mock_radarr.add_movie.side_effect = requests.ConnectionError("Connection refused")

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp1 = client.post(f"/download/{token}")

        # The call failed — should not be 200 ok
        assert resp1.json()["ok"] is False

        # Token must have been released; a second attempt must not return 409
        mock_radarr2 = MagicMock()
        mock_radarr2.get_movie_by_tmdb.return_value = None
        mock_radarr2.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr2
        ):
            resp2 = client.post(f"/download/{token}")

        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True

    def test_token_released_on_sonarr_exception(
        self, app_factory, conn, secret_key, templates_stub
    ):
        import requests

        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key, title="Severance", media_type="tv", tmdb_id=99)

        mock_sonarr = MagicMock()
        # Use a transport-shaped exception — see test_token_released_on_radarr_exception.
        mock_sonarr.lookup_by_tmdb_id.side_effect = requests.Timeout("Timeout")

        with patch(
            "mediaman.web.routes.download.submit.build_sonarr_from_db", return_value=mock_sonarr
        ):
            resp1 = client.post(f"/download/{token}")

        assert resp1.json()["ok"] is False

        # Should be retryable
        mock_sonarr2 = MagicMock()
        mock_sonarr2.lookup_by_tmdb_id.return_value = [{"tvdbId": 777, "tmdbId": 99}]
        mock_sonarr2.add_series.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_sonarr_from_db", return_value=mock_sonarr2
        ):
            resp2 = client.post(f"/download/{token}")

        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True

    def test_token_not_released_on_409(self, app_factory, conn, secret_key, templates_stub):
        """A SafeHTTPError 409 from Radarr means the item already exists — token stays consumed."""
        from mediaman.services.infra import SafeHTTPError

        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://radarr/api/v3/movie"
        )

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp1 = client.post(f"/download/{token}")

        assert resp1.status_code == 409  # handler returns 409 when arr reports conflict
        assert resp1.json()["ok"] is False
        assert "already exists" in resp1.json()["error"]

        # Second attempt must be blocked — item exists, token is spent
        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp2 = client.post(f"/download/{token}")

        assert resp2.status_code == 409

    def test_redownload_token_released_on_409(self, app_factory, conn, secret_key, templates_stub):
        """For a re-download link, a 409 from Radarr means the item is already
        in the library — exactly the state the user wants to re-grab. The
        token MUST be released so the page can immediately retry rather than
        leaving the user stuck with 'link already used' on the next click."""
        from mediaman.services.infra import SafeHTTPError

        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        # Use the redownload action — same crypto helper, action="redownload".
        token = generate_download_token(
            email="test@example.com",
            action="redownload",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=None,
            secret_key=secret_key,
        )

        # First: Radarr says 409 (already exists). Redownload semantics say
        # "release the token so the page can re-issue the click".
        mock_radarr_409 = MagicMock()
        mock_radarr_409.get_movie_by_tmdb.return_value = None
        mock_radarr_409.add_movie.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://radarr/api/v3/movie"
        )
        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db",
            return_value=mock_radarr_409,
        ):
            resp1 = client.post(f"/download/{token}")
        assert resp1.status_code == 409
        assert "already exists" in resp1.json()["error"]

        # Second click on the same redownload link must succeed because
        # the token was released — the user is allowed to retry.
        mock_radarr_ok = MagicMock()
        mock_radarr_ok.get_movie_by_tmdb.return_value = None
        mock_radarr_ok.add_movie.return_value = None
        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db",
            return_value=mock_radarr_ok,
        ):
            resp2 = client.post(f"/download/{token}")
        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True


class TestPollingCapability:
    """POST /download/{token} issues a poll_token; status endpoint requires it."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_tokens._USED_TOKENS.clear()
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_submit._DOWNLOAD_LIMITER_POST.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_successful_post_returns_poll_token(
        self, app_factory, conn, secret_key, templates_stub
    ):
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "poll_token" in data
        # Validate the poll_token is scoped to the correct service + tmdb_id
        poll_payload = validate_poll_token(data["poll_token"], secret_key)
        assert poll_payload is not None
        assert poll_payload.get("svc") == "radarr"
        assert poll_payload.get("tmdb") == 42

    def test_poll_token_validates_wrong_service(
        self, app_factory, conn, secret_key, templates_stub
    ):
        """A radarr poll_token must not validate for sonarr."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        pt = resp.json()["poll_token"]
        poll_payload = validate_poll_token(pt, secret_key)
        assert poll_payload is not None
        # The token's svc/tmdb claim must not match wrong combinations.
        assert not (poll_payload.get("svc") == "sonarr" and poll_payload.get("tmdb") == 42)
        assert not (poll_payload.get("svc") == "radarr" and poll_payload.get("tmdb") == 99)

    def test_status_endpoint_accepts_poll_token(
        self, app_factory, conn, secret_key, templates_stub
    ):
        """GET /api/download/status with a valid poll_token returns 200 without admin session."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        # Mint a poll_token via a download
        token = _valid_token(secret_key, title="Dune", media_type="movie", tmdb_id=42)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            post_resp = client.post(f"/download/{token}")

        poll_token = post_resp.json()["poll_token"]

        mock_radarr2 = MagicMock()
        mock_radarr2.get_movie_by_tmdb.return_value = {
            "hasFile": True,
            "title": "Dune",
            "images": [],
        }
        mock_radarr2.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr2
        ):
            status_resp = client.get(
                "/api/download/status",
                params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
            )

        assert status_resp.status_code == 200
        assert status_resp.json()["state"] == "ready"

    def test_status_endpoint_rejects_mismatched_poll_token(
        self, app_factory, conn, secret_key, templates_stub
    ):
        """A poll_token for tmdb_id=99 must be rejected for a query on tmdb_id=42."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        # Mint token for tmdb_id=99
        token99 = _valid_token(secret_key, title="Other", media_type="movie", tmdb_id=99)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            post_resp = client.post(f"/download/{token99}")

        poll_token_for_99 = post_resp.json()["poll_token"]

        # Try to use it to query tmdb_id=42
        status_resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token_for_99},
        )
        assert status_resp.status_code == 401

    def test_status_endpoint_rejects_no_auth(self, app_factory, conn, templates_stub):
        """GET /api/download/status with no token of any kind returns 401."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42},
        )
        assert resp.status_code == 401


class TestYoutubeTrailerKeyValidation:
    """Trailer_key must pass the YouTube ID regex before reaching the template."""

    def test_valid_youtube_id_passes_through(self):
        """A well-formed 11-char YouTube ID is returned unchanged."""
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_id_is_rejected(self):
        """An ID shorter than 11 chars is rejected."""
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id("short") is None

    def test_long_id_is_rejected(self):
        """An ID longer than 11 chars is rejected."""
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id("a" * 12) is None

    def test_id_with_slash_is_rejected(self):
        """An ID containing a slash (path traversal) is rejected."""
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id("abc/def1234") is None

    def test_id_with_angle_brackets_is_rejected(self):
        """An ID containing HTML metacharacters is rejected."""
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id("<script>abc") is None

    def test_none_input_returns_none(self):
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id(None) is None

    def test_empty_string_returns_none(self):
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id("") is None

    def test_valid_id_with_dash_and_underscore(self):
        """YouTube IDs may contain - and _ characters."""
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id("abc-efg_hij") == "abc-efg_hij"

    def test_suggestion_trailer_key_validated_on_page_load(
        self, app_factory, conn, secret_key, templates_stub
    ):
        """A malicious trailer_key from the DB is sanitised to None before rendering."""
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app)

        # Insert a suggestion with a malicious trailer_key
        sid = insert_suggestion(
            conn,
            title="Dune",
            media_type="movie",
            category="personal",
            tmdb_id=42,
            trailer_key="<script>evil</script>",
            created_at="2026-01-01",
        )

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

    def test_the_page_gets_the_stored_backdrop_and_a_whole_summary(
        self, app_factory, conn, secret_key, templates_stub
    ):
        cut = ("A first sentence. " + "and so on " * 30)[:250]
        assert len(cut) == 250
        wide = "https://image.tmdb.org/t/p/w1280/wide.jpg"
        sid = insert_suggestion(
            conn, title="Dune", tmdb_id=42, description=cut, created_at="2026-01-01"
        )
        with conn:
            conn.execute("UPDATE suggestions SET backdrop_url = ? WHERE id = ?", (wide, sid))
        token = generate_download_token(
            email="test@example.com",
            action="download",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=sid,
            secret_key=secret_key,
        )
        app = app_factory(download_router, conn=conn, state_extras={"templates": templates_stub})

        ctx = TestClient(app).get(f"/download/{token}").json()

        assert ctx["item"]["backdrop_url"] == wide
        assert ctx["hero_backdrop"] == wide
        assert ctx["item"]["description"] == "A first sentence."


def _app_with_real_templates(app_factory, conn):
    from pathlib import Path

    from fastapi.templating import Jinja2Templates

    template_dir = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "templates"
    return app_factory(
        download_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(template_dir))},
    )


def _render_queued_page(
    app_factory, conn, secret_key, email: str | None, recommendation_id: int | None = None
) -> str:
    app = _app_with_real_templates(app_factory, conn)
    token = generate_download_token(
        email=email,
        action="download",
        title="Dune",
        media_type="movie",
        tmdb_id=42,
        recommendation_id=recommendation_id,
        secret_key=secret_key,
    )
    queued_in_radarr = {
        "radarr_movies": {42: {"hasFile": False, "monitored": True}},
        "radarr_queue_tmdb_ids": set(),
    }
    with patch.object(_download_confirm, "_get_radarr_cache_cached", return_value=queued_in_radarr):
        resp = TestClient(app).get(f"/download/{token}")
    assert resp.status_code == 200
    assert "dl-hint" in resp.text
    return resp.text


class TestDownloadPageEmailPromise:
    """The page only promises an email that will come."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_link_without_email_promises_no_email(self, app_factory, conn, secret_key):
        page = _render_queued_page(app_factory, conn, secret_key, email=None)

        assert "notified by email" not in page
        assert '"notify_by_email": false' in page

    def test_a_queued_title_the_person_asked_for_promises_one(self, app_factory, conn, secret_key):
        with conn:
            record_download_notification(
                conn,
                email="a@example.com",
                title="Dune",
                media_type="movie",
                tmdb_id=42,
                service="radarr",
            )

        page = _render_queued_page(app_factory, conn, secret_key, email="a@example.com")

        assert "notified by email" in page
        assert '"notify_by_email": true' in page

    def test_a_queued_title_they_did_not_ask_for_promises_none(self, app_factory, conn, secret_key):
        with conn:
            record_download_notification(
                conn,
                email="other@example.com",
                title="Dune",
                media_type="movie",
                tmdb_id=42,
                service="radarr",
            )

        page = _render_queued_page(app_factory, conn, secret_key, email="a@example.com")

        assert "notified by email" not in page
        assert '"notify_by_email": false' in page


class TestDownloadPageBackdrop:
    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_the_script_is_given_the_wide_backdrop_for_the_card_it_builds(
        self, app_factory, conn, secret_key
    ):
        wide = "https://image.tmdb.org/t/p/w1280/wide.jpg"
        sid = insert_suggestion(conn, title="Dune", tmdb_id=42, created_at="2026-01-01")
        with conn:
            conn.execute("UPDATE suggestions SET backdrop_url = ? WHERE id = ?", (wide, sid))

        page = _render_queued_page(app_factory, conn, secret_key, email=None, recommendation_id=sid)

        bootstrap = re.search(r'<script id="page-bootstrap"[^>]*>(.*?)</script>', page, re.S)
        assert bootstrap is not None
        assert json.loads(bootstrap.group(1))["backdrop_url"] == wide


class TestDownloadPageTrailer:
    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_trailer_iframe_uses_no_cookie_origin(self, app_factory, conn, secret_key):
        sid = insert_suggestion(
            conn,
            title="Dune",
            media_type="movie",
            category="personal",
            tmdb_id=42,
            trailer_key="dQw4w9WgXcQ",
            created_at="2026-01-01",
        )

        page = _render_queued_page(app_factory, conn, secret_key, email=None, recommendation_id=sid)

        iframe = re.search(r'<iframe src="([^"]+)"', page)
        assert iframe is not None
        assert iframe.group(1) == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?rel=0"
        sandbox = re.search(r'<iframe[^>]*\ssandbox="([^"]+)"', page)
        assert sandbox is not None
        # popups let the player's own "Watch on YouTube" link open, outside the sandbox
        assert set(sandbox.group(1).split()) == {
            "allow-scripts",
            "allow-same-origin",
            "allow-presentation",
            "allow-popups",
            "allow-popups-to-escape-sandbox",
        }


class TestDownloadPageCast:
    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_the_stored_cast_is_shown_and_a_broken_entry_is_left_out(
        self, app_factory, conn, secret_key
    ):
        sid = insert_suggestion(
            conn,
            title="Dune",
            media_type="movie",
            category="personal",
            tmdb_id=42,
            cast_json='[{"name": "Ada Vale", "character": "Lead"}, {"name": null}, 7]',
            created_at="2026-01-01",
        )

        page = _render_queued_page(app_factory, conn, secret_key, email=None, recommendation_id=sid)

        assert re.findall(r'class="cast-name">([^<]*)<', page) == ["Ada Vale"]
        assert re.findall(r'class="cast-char">([^<]*)<', page) == ["Lead"]


class TestDownloadPageReason:
    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()

    @pytest.mark.parametrize(("category", "shown"), [("trending", True), ("personal", False)])
    def test_a_reason_made_from_everyones_viewing_is_not_shown(
        self, app_factory, conn, secret_key, category, shown
    ):
        sid = insert_suggestion(
            conn,
            title="Dune",
            category=category,
            tmdb_id=42,
            reason="A quiet heist.",
            director="D. V.",
        )

        page = _render_queued_page(app_factory, conn, secret_key, email=None, recommendation_id=sid)

        assert ("A quiet heist." in page) is shown
        assert "D. V." in page


class TestDownloadPageOfAnOwnPick:
    """Its link names the title and no stored pick."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_it_has_the_titles_facts_and_neither_a_reason_nor_anyones_name(
        self, app_factory, conn, secret_key
    ):
        insert_suggestion(
            conn,
            title="Dune",
            tmdb_id=42,
            poster_url="https://image.tmdb.org/t/p/w500/p.jpg",
            description="A desert planet.",
            reason="A reason of a shared pick.",
        )
        insert_admin_user(conn, username="amyowner", role="viewer")
        insert_user_pick(conn, "amyowner", title="Dune", tmdb_id=42, reason="Amys reason.")

        page = _render_queued_page(app_factory, conn, secret_key, email="a@example.com")

        assert "https://image.tmdb.org/t/p/w500/p.jpg" in page
        assert "A desert planet." in page
        assert "AI pick" in page
        for private in ("A reason of a shared pick.", "Amys reason.", "amyowner"):
            assert private not in page

    def test_a_title_not_stored_is_asked_of_tmdb_by_its_id(self, app_factory, conn, secret_key):
        insert_suggestion(conn, title="Dune", tmdb_id=7, description="Another Dune.")
        tmdb = MagicMock(**{"details.return_value": {"id": 42, "overview": "From TMDB."}})
        tmdb.search.side_effect = AssertionError("a title can be a namesake's")
        with patch("mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb):
            page = _render_queued_page(app_factory, conn, secret_key, email=None)

        assert "From TMDB." in page
        assert "Another Dune." not in page
        assert {c.args for c in tmdb.details.call_args_list} == {("movie", 42)}

    def test_a_redownload_is_no_ai_pick(self, app_factory, conn, secret_key):
        insert_suggestion(conn, title="Dune", tmdb_id=42, poster_url="https://x/p.jpg")
        token = generate_download_token(
            email="a@example.com",
            action="redownload",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=None,
            secret_key=secret_key,
        )
        page = TestClient(_app_with_real_templates(app_factory, conn)).get(f"/download/{token}")
        assert "Dune" in page.text
        assert "AI pick" not in page.text


class TestRateLimitedPage:
    def test_it_is_a_styled_429_that_offers_the_same_link_again(self, app_factory, conn):
        client = TestClient(_app_with_real_templates(app_factory, conn))
        with patch.object(_download_confirm._DOWNLOAD_LIMITER_GET, "check", return_value=False):
            resp = client.get("/download/any-token")

        assert resp.status_code == 429
        assert "/static/css/" in resp.text
        assert '<h1 class="dl-expired-title">Too many requests</h1>' in resp.text
        assert '<a href="">Try again</a>' in resp.text
        assert "expired" not in resp.text.split("<title>")[1].split("</title>")[0].lower()
