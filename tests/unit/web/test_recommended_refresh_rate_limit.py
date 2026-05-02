"""Tests for the manual /api/recommended/refresh rate limit and recommended-download
security hardening: on-demand share tokens (C18) and download rate limiting (C38).

OpenAI tokens cost real money, so a single user spamming the button
or scripting the endpoint must not be able to trigger more than one
refresh per 24-hour window. The limit is enforced server-side; the UI
just hides the button to match.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.services.openai.recommendations import throttle as _throttle
from mediaman.web.routes.recommended import api as _rec_api
from mediaman.web.routes.recommended import refresh as _rec_refresh


@pytest.fixture
def app(db_path, secret_key):
    conn = init_db(str(db_path))
    set_connection(conn)
    application = create_app()
    application.state.config = MagicMock(
        secret_key=secret_key,
        data_dir=str(db_path.parent),
    )
    application.state.db = conn
    application.state.db_path = str(db_path)
    yield application
    conn.close()


@pytest.fixture
def authed_client(app):
    from mediaman.auth.session import create_session, create_user

    conn = app.state.db
    create_user(conn, "testadmin", "testpass123", enforce_policy=False)
    token = create_session(conn, "testadmin")
    client = TestClient(app)
    client.cookies.set("session_token", token)
    client.headers.update({"Origin": "http://testserver"})
    return client


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """Each test starts with a clean result cache so that background-state
    from a prior test can't leak into this one. The running flag is now
    DB-backed so no module-level reset is needed."""
    _rec_refresh._refresh_result = None
    yield
    _rec_refresh._refresh_result = None


def _stamp_last_refresh(conn, when: datetime) -> None:
    """Pretend a manual refresh just ran at the given time."""
    _throttle.record_manual_refresh(conn, when)


# ---------------------------------------------------------------------------
# Cooldown helpers (pure functions — no FastAPI app needed)
# ---------------------------------------------------------------------------


class TestCooldownHelpers:
    def test_no_prior_refresh_means_no_cooldown(self, db_path):
        conn = init_db(str(db_path))
        try:
            assert _throttle.refresh_cooldown_remaining(conn) is None
        finally:
            conn.close()

    def test_recent_refresh_returns_remaining_time(self, db_path):
        conn = init_db(str(db_path))
        try:
            _stamp_last_refresh(conn, datetime.now(timezone.utc) - timedelta(hours=1))
            remaining = _throttle.refresh_cooldown_remaining(conn)
            assert remaining is not None
            # Should be within (22h, 23h] — give a generous margin to
            # avoid clock-skew flakes.
            assert timedelta(hours=22) < remaining <= timedelta(hours=23)
        finally:
            conn.close()

    def test_expired_cooldown_returns_none(self, db_path):
        conn = init_db(str(db_path))
        try:
            _stamp_last_refresh(conn, datetime.now(timezone.utc) - timedelta(hours=25))
            assert _throttle.refresh_cooldown_remaining(conn) is None
        finally:
            conn.close()

    def test_corrupt_timestamp_treated_as_no_refresh(self, db_path):
        conn = init_db(str(db_path))
        try:
            conn.execute(
                "INSERT INTO settings (key, value, encrypted, updated_at) "
                "VALUES ('last_manual_recommendation_refresh', 'not-a-date', 0, '2026-01-01')"
            )
            conn.commit()
            assert _throttle.refresh_cooldown_remaining(conn) is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# /api/recommended/refresh enforcement
# ---------------------------------------------------------------------------


class TestRefreshRateLimit:
    def test_second_call_within_24h_returns_429(self, authed_client, app):
        conn = app.state.db
        # Pretend the first manual refresh ran an hour ago.
        _stamp_last_refresh(conn, datetime.now(timezone.utc) - timedelta(hours=1))

        resp = authed_client.post("/api/recommended/refresh")
        assert resp.status_code == 429
        body = resp.json()
        assert body["ok"] is False
        assert "cooldown_seconds" in body
        assert "next_available_at" in body
        # Cooldown should be in the (22h, 23h] window.
        assert 22 * 3600 < body["cooldown_seconds"] <= 23 * 3600

    def test_call_after_cooldown_is_allowed(self, authed_client, app):
        conn = app.state.db
        _stamp_last_refresh(conn, datetime.now(timezone.utc) - timedelta(hours=25))

        # Stub the Plex client lookup so the request gets past the
        # "Plex not configured" early-return — cooldown logic is the
        # only thing under test here.
        with patch(
            "mediaman.services.arr.build.build_plex_from_db",
            return_value=MagicMock(),
        ):
            resp = authed_client.post("/api/recommended/refresh")

        assert resp.status_code == 200
        body = resp.json()
        # Either the background job started, or no plex configured.
        # We only care that the 429 cooldown branch wasn't hit.
        assert body.get("status") == "started" or "Plex" in body.get("error", "")

    def test_first_call_is_allowed(self, authed_client, app):
        # Brand-new DB — no previous refresh recorded.
        with patch(
            "mediaman.services.arr.build.build_plex_from_db",
            return_value=MagicMock(),
        ):
            resp = authed_client.post("/api/recommended/refresh")
        assert resp.status_code == 200

    def test_unauth_call_rejected(self, app):
        # No session cookie — must not even reach the cooldown check.
        client = TestClient(app)
        resp = client.post("/api/recommended/refresh", follow_redirects=False)
        assert resp.status_code in (302, 303, 401, 403)

    def test_status_endpoint_reports_cooldown(self, authed_client, app):
        conn = app.state.db
        _stamp_last_refresh(conn, datetime.now(timezone.utc) - timedelta(hours=2))
        resp = authed_client.get("/api/recommended/refresh/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["manual_refresh_available"] is False
        assert body["cooldown_seconds"] > 0
        assert "next_available_at" in body

    def test_status_endpoint_reports_available_after_cooldown(self, authed_client, app):
        conn = app.state.db
        _stamp_last_refresh(conn, datetime.now(timezone.utc) - timedelta(hours=25))
        resp = authed_client.get("/api/recommended/refresh/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["manual_refresh_available"] is True
        assert "cooldown_seconds" not in body


# ---------------------------------------------------------------------------
# C38 — rate limit on /api/recommended/{id}/download
# ---------------------------------------------------------------------------


class TestDownloadActionRateLimit:
    """Authenticated admin download must be rate-limited at 30/min."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        """Clear the in-process rate-limiter state between tests."""
        _rec_api._DOWNLOAD_ACTION_LIMITER.reset()
        yield
        _rec_api._DOWNLOAD_ACTION_LIMITER.reset()

    def test_rate_limit_blocks_after_window_exceeded(self, authed_client, app):
        """Hammering the download endpoint more than 30 times/min returns 429."""
        conn = app.state.db
        # Insert a dummy suggestion so the 404 path isn't hit
        conn.execute(
            "INSERT INTO suggestions (title, media_type, category, tmdb_id, created_at) "
            "VALUES ('Dune', 'movie', 'personal', 42, '2026-01-01T00:00:00')"
        )
        conn.commit()

        max_in_window = _rec_api._DOWNLOAD_ACTION_LIMITER._max_in_window

        mock_radarr = MagicMock()
        mock_radarr.add_movie.side_effect = Exception("Not calling real Radarr")

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=mock_radarr
        ):
            for _ in range(max_in_window):
                r = authed_client.post("/api/recommended/1/download")
                # The mock raises so we get an error response, not 429
                assert r.status_code != 429, "Rate limit fired early"

            # One more — must hit the rate limit
            r = authed_client.post("/api/recommended/1/download")

        assert r.status_code == 429
        assert r.json()["ok"] is False

    def test_unauthenticated_request_rejected_before_rate_limit(self, app):
        """An unauthenticated call must be rejected with 302/401 not 429."""
        client = TestClient(app)
        resp = client.post("/api/recommended/1/download", follow_redirects=False)
        assert resp.status_code in (302, 303, 401, 403)


# ---------------------------------------------------------------------------
# C18 — on-demand share tokens via POST /api/recommended/{id}/share-token
# ---------------------------------------------------------------------------


class TestOnDemandShareToken:
    """Share tokens must be minted on demand, not pre-embedded in the page."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _rec_api._SHARE_TOKEN_LIMITER.reset()
        yield
        _rec_api._SHARE_TOKEN_LIMITER.reset()

    def _insert_suggestion(self, conn) -> int:
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('base_url', 'https://example.com', 0, '2026-01-01') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.execute(
            "INSERT INTO suggestions (title, media_type, category, tmdb_id, created_at) "
            "VALUES ('Interstellar', 'movie', 'personal', 157336, '2026-01-01T00:00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT id FROM suggestions WHERE title='Interstellar'").fetchone()
        return row["id"]

    def test_share_token_endpoint_returns_share_url(self, authed_client, app):
        """POST /api/recommended/{id}/share-token returns a valid share_url."""
        conn = app.state.db
        rec_id = self._insert_suggestion(conn)

        resp = authed_client.post(f"/api/recommended/{rec_id}/share-token")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "share_url" in body
        assert "/download/" in body["share_url"]
        assert "token" in body
        assert "expires_at" in body

    def test_share_token_endpoint_404_for_missing_rec(self, authed_client, app):
        resp = authed_client.post("/api/recommended/99999/share-token")
        assert resp.status_code == 404

    def test_share_token_endpoint_unauthenticated_rejects(self, app):
        client = TestClient(app)
        resp = client.post("/api/recommended/1/share-token", follow_redirects=False)
        assert resp.status_code in (302, 303, 401, 403)

    def test_share_token_rate_limited(self, authed_client, app):
        """Hammering share-token endpoint past 30/min returns 429."""
        conn = app.state.db
        rec_id = self._insert_suggestion(conn)

        max_in_window = _rec_api._SHARE_TOKEN_LIMITER._max_in_window
        for _ in range(max_in_window):
            r = authed_client.post(f"/api/recommended/{rec_id}/share-token")
            assert r.status_code != 429

        r = authed_client.post(f"/api/recommended/{rec_id}/share-token")
        assert r.status_code == 429

    def test_page_does_not_embed_share_urls(self, authed_client, app):
        """The recommended page JSON must not contain pre-minted share_url values."""
        conn = app.state.db
        self._insert_suggestion(conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = []

        with (
            patch(
                "mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=mock_radarr
            ),
            patch(
                "mediaman.web.routes.recommended.api.build_sonarr_from_db", return_value=MagicMock()
            ),
        ):
            resp = authed_client.get("/recommended", follow_redirects=True)

        assert resp.status_code == 200
        # The server-side data block must not contain any share_url keys
        body_text = resp.text
        # Parse the JSON block embedded in the HTML
        import re

        match = re.search(
            r'<script id="rec-data" type="application/json">(.*?)</script>',
            body_text,
            re.DOTALL,
        )
        assert match is not None, "rec-data script block not found"
        rec_data = __import__("json").loads(match.group(1))
        for item in rec_data.values():
            assert "share_url" not in item or item["share_url"] == "", (
                f"share_url must not be pre-embedded in page for item {item.get('title')}"
            )


# ---------------------------------------------------------------------------
# Success paths — _add_rec_to_radarr and _add_rec_to_sonarr
# ---------------------------------------------------------------------------


class TestDownloadRecommendationSuccess:
    """Happy-path coverage for POST /api/recommended/{id}/download."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _rec_api._DOWNLOAD_ACTION_LIMITER.reset()
        yield
        _rec_api._DOWNLOAD_ACTION_LIMITER.reset()

    def _insert_movie_suggestion(self, conn) -> int:
        conn.execute(
            "INSERT INTO suggestions (title, media_type, category, tmdb_id, created_at) "
            "VALUES ('Inception', 'movie', 'personal', 27205, '2026-01-01T00:00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT id FROM suggestions WHERE title='Inception'").fetchone()
        return row["id"]

    def _insert_tv_suggestion(self, conn) -> int:
        conn.execute(
            "INSERT INTO suggestions (title, media_type, category, tmdb_id, created_at) "
            "VALUES ('Breaking Bad', 'tv', 'personal', 1396, '2026-01-01T00:00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT id FROM suggestions WHERE title='Breaking Bad'").fetchone()
        return row["id"]

    def test_add_movie_to_radarr_succeeds(self, authed_client, app):
        """A valid movie suggestion is added to Radarr and returns ok=True."""
        conn = app.state.db
        rec_id = self._insert_movie_suggestion(conn)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None  # success — no exception

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "Inception" in body["message"]
        mock_radarr.add_movie.assert_called_once_with(27205, "Inception")

    def test_add_tv_to_sonarr_succeeds(self, authed_client, app):
        """A valid TV suggestion is added to Sonarr and returns ok=True."""
        conn = app.state.db
        rec_id = self._insert_tv_suggestion(conn)

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 81189}]
        mock_sonarr.add_series.return_value = None

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db", return_value=mock_sonarr
        ):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "Breaking Bad" in body["message"]
        mock_sonarr.add_series.assert_called_once_with(81189, "Breaking Bad")

    def test_missing_recommendation_returns_404(self, authed_client, app):
        """A request for a non-existent suggestion ID returns 404."""
        resp = authed_client.post("/api/recommended/99999/download")
        assert resp.status_code == 404
        assert resp.json()["ok"] is False

    def test_radarr_not_configured_returns_error(self, authed_client, app):
        """When Radarr is not configured the endpoint returns an error response."""
        conn = app.state.db
        rec_id = self._insert_movie_suggestion(conn)

        with patch("mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=None):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "Radarr" in body["error"]

    def test_sonarr_not_configured_returns_error(self, authed_client, app):
        """When Sonarr is not configured the endpoint returns an error response."""
        conn = app.state.db
        rec_id = self._insert_tv_suggestion(conn)

        with patch("mediaman.web.routes.recommended.api.build_sonarr_from_db", return_value=None):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "Sonarr" in body["error"]

    def test_radarr_http_409_treated_as_already_exists(self, authed_client, app):
        """A 409 SafeHTTPError from Radarr is surfaced as an 'already exists' error."""
        from mediaman.services.infra.http_client import SafeHTTPError

        conn = app.state.db
        rec_id = self._insert_movie_suggestion(conn)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://radarr/api/v3/movie"
        )

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "already" in body["error"].lower()

    def test_sonarr_409_safe_http_error_treated_as_already_exists(self, authed_client, app):
        """A 409 SafeHTTPError from Sonarr is surfaced as an 'already exists' error."""
        from mediaman.services.infra.http_client import SafeHTTPError

        conn = app.state.db
        rec_id = self._insert_tv_suggestion(conn)

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 81189}]
        mock_sonarr.add_series.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://sonarr/api/v3/series"
        )

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db", return_value=mock_sonarr
        ):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "already" in body["error"].lower()

    def test_downloaded_at_set_on_success(self, authed_client, app):
        """A successful download stamps ``downloaded_at`` on the suggestion row."""
        conn = app.state.db
        rec_id = self._insert_movie_suggestion(conn)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=mock_radarr
        ):
            authed_client.post(f"/api/recommended/{rec_id}/download")

        row = conn.execute(
            "SELECT downloaded_at FROM suggestions WHERE id = ?", (rec_id,)
        ).fetchone()
        assert row["downloaded_at"] is not None
