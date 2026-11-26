"""Tests for GET/POST /download/{token} — the token-gated download confirmation page."""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import generate_download_token, validate_poll_token
from mediaman.services.arr import ArrError
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.media_meta import item_enrichment
from mediaman.web.routes.download import _tokens as _download_tokens
from mediaman.web.routes.download import confirm as _download_confirm
from mediaman.web.routes.download import router as download_router
from mediaman.web.routes.download import submit as _download_submit
from tests.helpers.factories import (
    insert_admin_user,
    insert_settings,
    insert_suggestion,
    insert_user_pick,
)


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
        assert "already downloaded or on its way" in resp1.json()["error"]

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
        assert "already downloaded or on its way" in resp1.json()["error"]

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

        assert ctx["item"]["backdrop_url"].startswith("/api/img/tmdb/w1280/wide.jpg?sig=")
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
    assert "share-steps" in resp.text
    return resp.text


class TestDownloadPageEmailPromise:
    """The page only promises an email that will come."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_link_without_email_promises_no_email(self, app_factory, conn, secret_key):
        page = _render_queued_page(app_factory, conn, secret_key, email=None)

        assert "send you a mail" not in page
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

        assert "send you a mail" in page
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

        assert "send you a mail" not in page
        assert '"notify_by_email": false' in page


class TestDownloadPageBackdrop:
    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()

    def test_the_wide_backdrop_is_behind_a_title_on_its_way(self, app_factory, conn, secret_key):
        wide = "https://image.tmdb.org/t/p/w1280/wide.jpg"
        sid = insert_suggestion(conn, title="Dune", tmdb_id=42, created_at="2026-01-01")
        with conn:
            conn.execute("UPDATE suggestions SET backdrop_url = ? WHERE id = ?", (wide, sid))

        page = _render_queued_page(app_factory, conn, secret_key, email=None, recommendation_id=sid)

        art = '<img class="watch-banner__bg watch-banner__bg--art" src="/api/img/tmdb/w1280/wide.jpg?sig='
        assert art in page
        bootstrap = re.search(r'<script id="page-bootstrap"[^>]*>(.*?)</script>', page, re.S)
        assert bootstrap is not None
        assert set(json.loads(bootstrap.group(1))) == {
            "token",
            "poll_token",
            "service",
            "tmdb_id",
            "download_state",
            "notify_by_email",
        }


def _offer_page(app_factory, conn, secret_key, **suggestion) -> str:
    """The page of a title nothing holds yet."""
    _download_confirm._DOWNLOAD_LIMITER_GET.reset()
    backdrop = suggestion.pop("backdrop_url", None)
    sid = insert_suggestion(conn, title="Dune", tmdb_id=42, **suggestion)
    with conn:
        conn.execute("UPDATE suggestions SET backdrop_url = ? WHERE id = ?", (backdrop, sid))
    token = generate_download_token(
        email="amy@example.com",
        action="download",
        title="Dune",
        media_type="movie",
        tmdb_id=42,
        recommendation_id=sid,
        secret_key=secret_key,
    )
    nothing = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set()}
    with patch.object(_download_confirm, "_get_radarr_cache_cached", return_value=nothing):
        resp = TestClient(_app_with_real_templates(app_factory, conn)).get(f"/download/{token}")
    assert resp.status_code == 200 and 'id="btn-download"' in resp.text
    return resp.text


class TestDownloadPageTrailer:
    def test_the_trailer_is_a_link_and_the_page_itself_asks_youtube_nothing(
        self, app_factory, conn, secret_key
    ):
        page = _offer_page(app_factory, conn, secret_key, trailer_key="dQw4w9WgXcQ")

        assert "<iframe" not in page
        link = re.search(r'<a [^>]*data-trailer="dQw4w9WgXcQ"[^>]*>', page)
        assert link and 'href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"' in link[0]
        assert 'rel="noopener noreferrer"' in link[0]
        assert set(re.findall(r"[^\"]*youtube[^\"]*", page)) == {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        }

    @pytest.mark.parametrize("state", [None, "in_library", "tracked", "queued"])
    def test_every_state_but_on_its_way_has_the_trailer_and_its_script(self, state):
        page = _template_page(download_state=state, trailer_key="dQw4w9WgXcQ")

        offered = state != "queued"
        shown = re.sub(r"<template.*?</template>", "", page, flags=re.S)
        assert ("data-trailer" in shown) is offered
        assert ("/static/js/share.js" in page) is offered

    @pytest.mark.parametrize("state", [None, "in_library", "tracked", "queued"])
    def test_without_scripts_a_download_button_says_it_does_nothing(self, state):
        page = _template_page(download_state=state)

        assert ("<noscript>" in page) is (state is None)

    def test_a_title_with_no_trailer_has_no_button_for_one(self, app_factory, conn, secret_key):
        page = _offer_page(app_factory, conn, secret_key, trailer_key="<script>evil")

        assert "data-trailer" not in page and "evil" not in page


class TestDownloadPagePeople:
    def test_who_asked_for_it_is_not_on_the_page(self, app_factory, conn, secret_key):
        page = _offer_page(app_factory, conn, secret_key)

        assert "example.com" not in page and "Requested" not in page

    @pytest.mark.parametrize(
        ("media_type", "credit"), [("movie", "Directed by"), ("tv", "Created by")]
    )
    def test_a_show_is_created_and_a_film_directed(self, media_type, credit):
        page = _template_page(media_type=media_type, director="L. M.")

        assert f'<span class="ttl__credit-label">{credit}</span><span>L. M.</span>' in page


def _template_page(**item) -> str:
    from pathlib import Path

    import jinja2

    templates = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "templates"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(templates), autoescape=True)
    item = {"title": "Dune", "media_type": "movie", "tmdb_id": 42, **item}
    return env.get_template("download.html").render(state="confirm", item=item)


def _previews(page: str) -> dict[str, str]:
    return dict(
        re.findall(r'<meta (?:property|name)="((?:og|twitter):\w+)" content="([^"]*)">', page)
    )


class TestLinkPreview:
    _WIDE = "https://image.tmdb.org/t/p/w1280/wide.jpg"
    _POSTER = "https://image.tmdb.org/t/p/w300/p.jpg"

    def test_it_is_the_title_and_its_picture_from_this_site(self, app_factory, conn, secret_key):
        insert_settings(conn, base_url="https://media.example/")
        page = _offer_page(
            app_factory, conn, secret_key, poster_url=self._POSTER, backdrop_url=self._WIDE
        )

        preview = _previews(page)
        assert preview.pop("og:image").startswith(
            "https://media.example/api/img/tmdb/w1280/wide.jpg?sig="
        )
        assert preview == {
            "og:site_name": "mediaman",
            "og:type": "website",
            "og:title": "Dune",
            "twitter:card": "summary_large_image",
        }

    def test_with_no_wide_picture_it_has_the_poster(self, app_factory, conn, secret_key):
        insert_settings(conn, base_url="https://media.example")
        page = _offer_page(app_factory, conn, secret_key, poster_url=self._POSTER)

        preview = _previews(page)
        assert preview["og:image"].startswith("https://media.example/api/img/tmdb/w500/p.jpg?sig=")
        assert preview["twitter:card"] == "summary"

    def test_with_no_site_address_it_has_no_picture(self, app_factory, conn, secret_key):
        page = _offer_page(
            app_factory, conn, secret_key, poster_url=self._POSTER, backdrop_url=self._WIDE
        )

        assert "og:image" not in _previews(page)
        assert _previews(page)["twitter:card"] == "summary"

    def test_it_names_the_season(self):
        assert _previews(_template_page(season_number=2))["og:title"] == "Dune · Season 2"

    def test_a_picture_of_another_host_and_the_mail_s_reader_are_not_in_it(
        self, app_factory, conn, secret_key
    ):
        insert_settings(conn, base_url="https://media.example")
        page = _offer_page(
            app_factory,
            conn,
            secret_key,
            poster_url="https://elsewhere.example/p.jpg",
            reason="Because.",
        )

        assert "og:image" not in _previews(page) and "elsewhere.example" not in page
        assert not {"Because.", "amy@example.com"} & set(_previews(page).values())

    def test_a_title_cannot_leave_its_attribute(self):
        page = _template_page(title='"><script>x</script>')

        assert "<script>x" not in page

    def test_a_dead_link_has_none(self, app_factory, conn):
        page = TestClient(_app_with_real_templates(app_factory, conn)).get("/download/bad").text

        assert _previews(page) == {} and "This link does not work any more" in page

    def test_a_chat_that_fetches_it_leaves_the_link_unspent(self, app_factory, conn, secret_key):
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        _download_confirm._reset_arr_cache_for_tests()
        _download_tokens._USED_TOKENS.clear()
        client = TestClient(_app_with_real_templates(app_factory, conn))
        token = _valid_token(secret_key)
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None
        before = list(conn.iterdump())
        with (
            patch.object(_download_confirm, "build_radarr_from_db", return_value=None),
            patch.object(_download_submit, "build_radarr_from_db", return_value=radarr),
        ):
            for _ in range(2):
                assert client.get(f"/download/{token}", headers={"user-agent": "bot"}).is_success
            assert list(conn.iterdump()) == before
            assert client.post(f"/download/{token}").json()["ok"] is True


class TestWatchLink:
    @pytest.mark.parametrize(
        ("item", "href"),
        [
            ({}, "/watch#title=tmdb-movie-42"),
            ({"media_type": "tv"}, "/watch#title=tmdb-tv-42"),
            ({"tmdb_id": None}, "/watch"),
            ({"tmdb_id": "//evil.example"}, "/watch"),
        ],
    )
    def test_it_opens_the_title_itself_on_this_site(self, item, href):
        page = _template_page(download_state="in_library", **item)

        assert f'<a class="btn btn--lg watch-btn--play" href="{href}">' in page
        assert "evil" not in page.split("page-bootstrap")[0]

    def test_its_title_is_one_watch_opens(self):
        from mediaman.web.auth.return_path import safe_return_path
        from mediaman.web.routes.watch.catalog_api import REF_RE

        href = re.search(
            r'watch-btn--play" href="([^"]+)"', _template_page(download_state="in_library")
        )
        assert href is not None
        path, _, ref = href[1].partition("#title=")
        assert REF_RE.fullmatch(ref) and safe_return_path(path) == "/watch"


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

        assert re.findall(r'class="ttl-cast__name">([^<]*)<', page) == ["Ada Vale"]
        assert re.findall(r'class="ttl-cast__char">([^<]*)<', page) == ["Lead"]
        assert "AV</span></span></span>" in page  # no photo: the letters stand

    @pytest.mark.parametrize(
        ("path", "photo"),
        [
            ("/ada.jpg", "/ada.jpg"),
            ("/../../etc/ada.jpg", None),
            ("//evil.example/ada.jpg", None),
            ('/a".jpg', None),
            (7, None),
            (None, None),
        ],
    )
    def test_a_photo_is_tmdbs_or_none(self, path, photo):
        (person,) = _download_confirm._coerce_cast([{"name": "Ada Vale", "profile_path": path}])

        assert person["photo_url"] == photo

    def test_the_photo_tmdb_gives_is_stored_and_a_person_with_none_has_no_entry_for_it(self):
        from mediaman.services.media_meta.tmdb import TmdbClient

        people = [
            {"name": "Ada Vale", "character": "Lead", "profile_path": "/ada.jpg"},
            {"name": "Bo Ek", "character": "", "profile_path": None},
        ]
        detail = TmdbClient.shape_detail({"id": 1, "credits": {"cast": people}}, media_type="tv")

        assert json.loads(detail["cast_json"] or "") == [
            people[0],
            {"name": "Bo Ek", "character": ""},
        ]

    def test_a_stored_photo_is_shown_and_does_not_name_the_page_to_its_host(
        self, app_factory, conn, secret_key
    ):
        page = _offer_page(
            app_factory,
            conn,
            secret_key,
            cast_json='[{"name": "Ada Vale", "character": "Lead", "profile_path": "/ada.jpg"}]',
        )

        photo = re.search(r'<img src="/api/img/tmdb/w185/ada.jpg\?sig=[^"]+"[^>]*>', page)
        assert photo and 'referrerpolicy="no-referrer"' in photo[0] and 'alt=""' in photo[0]


class TestDownloadPagePictures:
    """A visitor's browser takes every picture from this site, never from TMDB."""

    def _page(self, app_factory, conn, secret_key) -> tuple[str, str]:
        _download_confirm._DOWNLOAD_LIMITER_GET.reset()
        sid = insert_suggestion(
            conn,
            title="Dune",
            tmdb_id=42,
            poster_url="https://image.tmdb.org/t/p/w300/p.jpg",
            cast_json='[{"name": "Ada Vale", "character": "Lead", "profile_path": "/ada.jpg"}]',
        )
        with conn:
            conn.execute(
                "UPDATE suggestions SET backdrop_url = ? WHERE id = ?",
                ("https://image.tmdb.org/t/p/w1280/wide.jpg", sid),
            )
        token = generate_download_token(
            email=None,
            action="download",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=sid,
            secret_key=secret_key,
        )
        nothing = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set()}
        with patch.object(_download_confirm, "_get_radarr_cache_cached", return_value=nothing):
            page = TestClient(_app_with_real_templates(app_factory, conn)).get(f"/download/{token}")
        return page.text, token

    def test_no_picture_is_tmdbs_and_none_carries_the_link(self, app_factory, conn, secret_key):
        insert_settings(conn, base_url="https://media.example")
        page, token = self._page(app_factory, conn, secret_key)

        sources = re.findall(r'<img [^>]*src="([^"]*)"', page)
        assert {src.split("?")[0] for src in sources} == {
            "/api/img/tmdb/w1280/wide.jpg",
            "/api/img/tmdb/w500/p.jpg",
            "/api/img/tmdb/w185/ada.jpg",
        }
        assert "image.tmdb.org" not in page
        assert not [src for src in sources if token in src]

    def test_a_pictures_signature_lasts_as_long_as_the_link(self, app_factory, conn, secret_key):
        from mediaman.crypto import validate_download_token, validate_tmdb_image_token

        page, token = self._page(app_factory, conn, secret_key)

        link = validate_download_token(token, secret_key)
        assert link is not None
        sigs = set(re.findall(r'src="/api/img/tmdb/[^"]*\?sig=([^"]+)"', page))
        assert len(sigs) == 3
        for sig in sigs:
            picture = validate_tmdb_image_token(sig, secret_key)
            assert picture is not None
            assert link["exp"] <= picture["exp"] < link["exp"] + 86400


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


def test_a_genre_is_shown_once(app_factory, conn, secret_key):
    _download_confirm._DOWNLOAD_LIMITER_GET.reset()
    sid = insert_suggestion(
        conn, title="Dune", tmdb_id=42, genres='["Drama", "Comedy", "drama ", " Drama"]'
    )
    page = _render_queued_page(app_factory, conn, secret_key, email=None, recommendation_id=sid)
    assert "<span>Drama, Comedy</span>" in page


class TestDownloadPageOfAnOwnPick:
    """Its link names the title and no stored pick."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        item_enrichment._facts_cache.clear()
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

        assert "/api/img/tmdb/w500/p.jpg?sig=" in page
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


class TestUsedLink:
    """A used link whose title cannot be read offers no download."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        _download_tokens.reset_used_tokens()
        _download_confirm._reset_arr_cache_for_tests()

    def _page(self, app_factory, conn, token, **radarr):
        client = TestClient(_app_with_real_templates(app_factory, conn))
        with patch.object(_download_confirm, "_get_radarr_cache_cached", **radarr):
            return client.get(f"/download/{token}").text

    def _used(self, app_factory, conn, secret_key):
        app_factory(download_router, conn=conn)
        token = _valid_token(secret_key)
        assert _download_tokens._mark_token_used(token, 4102444800)
        # As after a restart.
        _download_tokens.reset_used_tokens()
        return token

    @pytest.mark.parametrize("radarr", [{"side_effect": ArrError("down")}, {"return_value": {}}])
    def test_a_used_link_says_so_and_has_no_button(self, app_factory, conn, secret_key, radarr):
        page = self._page(app_factory, conn, self._used(app_factory, conn, secret_key), **radarr)

        assert "data-dl" not in page.split("<template")[0]
        assert "used already" in page

    def test_a_used_link_whose_title_nothing_holds_offers_no_reload(
        self, app_factory, conn, secret_key
    ):
        token = self._used(app_factory, conn, secret_key)
        page = self._page(app_factory, conn, token, return_value={}).split("<template")[0]

        assert "used already" in page and "Reload" not in page

    def test_a_used_link_whose_add_got_no_answer_offers_a_reload(
        self, app_factory, conn, secret_key
    ):
        token = self._used(app_factory, conn, secret_key)
        _download_tokens.note_unanswered_add(conn, token, 4102444800)
        page = self._page(app_factory, conn, token, return_value={}).split("<template")[0]

        assert "used already" in page and "Reload" in page

    def test_a_used_link_whose_arr_cannot_be_read_offers_a_reload(
        self, app_factory, conn, secret_key
    ):
        token = self._used(app_factory, conn, secret_key)
        page = self._page(app_factory, conn, token, side_effect=ArrError("down"))

        assert "Reload" in page.split("<template")[0]

    def test_a_used_link_still_shows_what_the_title_is_doing(self, app_factory, conn, secret_key):
        token = self._used(app_factory, conn, secret_key)
        held = {"radarr_movies": {42: {"hasFile": False, "monitored": True}}}
        page = self._page(app_factory, conn, token, return_value=held)

        assert "is-searching" in page and "used already" not in page

    def test_a_link_that_failed_and_was_given_back_offers_its_button(
        self, app_factory, conn, secret_key
    ):
        token = self._used(app_factory, conn, secret_key)
        _download_tokens._unmark_token_used(token)
        page = self._page(app_factory, conn, token, side_effect=ArrError("down"))

        assert 'id="btn-download"' in page and "used already" not in page


class TestRateLimitedPage:
    def test_it_is_a_styled_429_that_offers_the_same_link_again(self, app_factory, conn):
        client = TestClient(_app_with_real_templates(app_factory, conn))
        with patch.object(_download_confirm._DOWNLOAD_LIMITER_GET, "check", return_value=False):
            resp = client.get("/download/any-token")

        assert resp.status_code == 429
        assert "/static/css/" in resp.text
        assert "<h1>One moment</h1>" in resp.text
        assert resp.headers["retry-after"] == "60"
        assert re.search(r'<a [^>]*href="" data-count="60">Try again</a>', resp.text)
        assert "expired" not in resp.text.split("<title>")[1].split("</title>")[0].lower()
