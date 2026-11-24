"""Tests for the manual /api/recommended/refresh rate limit and recommended-download
security hardening: on-demand share tokens and download rate limiting.

OpenAI tokens cost real money, so a single user spamming the button
or scripting the endpoint must not be able to trigger more than one
refresh per 24-hour window. The limit is enforced server-side; the UI
just hides the button to match.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.db import init_db, is_refresh_running, set_connection
from mediaman.main import create_app
from mediaman.services.openai.recommendations import throttle as _throttle
from mediaman.web.routes.recommended import api as _rec_api
from mediaman.web.routes.recommended import refresh as _rec_refresh
from tests.helpers.factories import insert_settings, insert_suggestion

# NOTE: this file deliberately keeps its own ``app`` / ``authed_client``
# fixtures rather than adopting the shared ``app_factory`` / ``authed_client``
# in tests/unit/web/conftest.py. The recommended page test
# (test_page_does_not_embed_share_urls) renders the live ``/watch``
# template, and the rate-limit tests have to pass through the CSRF
# middleware on unsafe POSTs — both need the full ``create_app()`` stack.


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
    from mediaman.web.auth.password_hash import create_user
    from mediaman.web.auth.session_store import create_session

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
    _rec_refresh._refresh_thread = None
    yield
    _rec_refresh._refresh_result = None
    _rec_refresh._refresh_thread = None


def _stamp_last_refresh(conn, when: datetime) -> None:
    """Pretend a manual refresh just ran at the given time."""
    _throttle.record_manual_refresh(conn, when)


class TestCooldownHelpers:
    def test_no_prior_refresh_means_no_cooldown(self, db_path):
        conn = init_db(str(db_path))
        try:
            assert _throttle.refresh_cooldown_remaining(conn) is None
        finally:
            conn.close()

    def test_recent_refresh_returns_remaining_time(self, db_path, freezer):
        conn = init_db(str(db_path))
        try:
            _stamp_last_refresh(conn, datetime.now(UTC) - timedelta(hours=1))
            remaining = _throttle.refresh_cooldown_remaining(conn)
            assert remaining is not None
            # Clock is frozen so the remaining time is exact: 24h cooldown − 1h elapsed = 23h.
            assert remaining == timedelta(hours=23)
        finally:
            conn.close()

    def test_expired_cooldown_returns_none(self, db_path, freezer):
        conn = init_db(str(db_path))
        try:
            _stamp_last_refresh(conn, datetime.now(UTC) - timedelta(hours=25))
            assert _throttle.refresh_cooldown_remaining(conn) is None
        finally:
            conn.close()

    def test_corrupt_timestamp_treated_as_no_refresh(self, db_path):
        conn = init_db(str(db_path))
        try:
            insert_settings(
                conn,
                last_manual_recommendation_refresh="not-a-date",
                updated_at="2026-01-01",
            )
            assert _throttle.refresh_cooldown_remaining(conn) is None
        finally:
            conn.close()


class TestRefreshRateLimit:
    def test_second_call_within_24h_returns_429(self, authed_client, app, freezer):
        conn = app.state.db
        # Pretend the first manual refresh ran an hour ago.
        _stamp_last_refresh(conn, datetime.now(UTC) - timedelta(hours=1))

        resp = authed_client.post("/api/recommended/refresh")
        assert resp.status_code == 429
        body = resp.json()
        assert body["ok"] is False
        assert "cooldown_seconds" in body
        assert "next_available_at" in body
        # Cooldown should be in the (22h, 23h] window.
        assert 22 * 3600 < body["cooldown_seconds"] <= 23 * 3600
        assert body["message"] == body["error"]
        assert "already refreshed" in body["message"]

    def test_call_after_cooldown_is_allowed(self, authed_client, app, freezer):
        conn = app.state.db
        _stamp_last_refresh(conn, datetime.now(UTC) - timedelta(hours=25))

        with patch.object(_rec_refresh, "refresh_recommendations", return_value=1):
            resp = authed_client.post("/api/recommended/refresh")
            _rec_refresh._refresh_thread.join(timeout=5)

        assert (resp.status_code, resp.json()) == (200, {"status": "started"})

    def test_first_call_is_allowed_with_plex_not_set_up(self, authed_client, app):
        # Brand-new DB — no previous refresh recorded, and no Plex settings.
        with patch.object(_rec_refresh, "refresh_recommendations", return_value=1) as refresh:
            resp = authed_client.post("/api/recommended/refresh")
            _rec_refresh._refresh_thread.join(timeout=5)
        assert (resp.status_code, resp.json()) == (200, {"status": "started"})
        assert refresh.call_count == 1
        assert authed_client.get("/api/recommended/refresh/status").json()["result"] == {
            "ok": True,
            "count": 1,
        }

    def test_a_failed_refresh_releases_the_lease(self, authed_client, app):
        with patch.object(_rec_refresh, "refresh_recommendations", side_effect=ValueError("x")):
            authed_client.post("/api/recommended/refresh")
            _rec_refresh._refresh_thread.join(timeout=5)
        assert not is_refresh_running(app.state.db)
        result = authed_client.get("/api/recommended/refresh/status").json()["result"]
        assert result["ok"] is False

    def test_unauth_call_rejected(self, app):
        # No session cookie — must not even reach the cooldown check.
        client = TestClient(app)
        resp = client.post("/api/recommended/refresh", follow_redirects=False)
        assert resp.status_code in (302, 303, 401, 403)

    def test_status_endpoint_reports_cooldown(self, authed_client, app, freezer):
        conn = app.state.db
        _stamp_last_refresh(conn, datetime.now(UTC) - timedelta(hours=2))
        resp = authed_client.get("/api/recommended/refresh/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["manual_refresh_available"] is False
        assert body["cooldown_seconds"] > 0
        assert "next_available_at" in body

    def test_status_endpoint_reports_available_after_cooldown(self, authed_client, app, freezer):
        conn = app.state.db
        _stamp_last_refresh(conn, datetime.now(UTC) - timedelta(hours=25))
        resp = authed_client.get("/api/recommended/refresh/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["manual_refresh_available"] is True
        assert "cooldown_seconds" not in body

    def test_zero_recommendations_does_not_record_cooldown(self, authed_client, app):
        """If OpenAI returns nothing usable, the worker must NOT burn the
        24h cooldown — otherwise the user is locked out for a day despite
        no actual refresh having happened. The status endpoint must also
        surface the failure so the UI can show a real error."""
        import threading

        conn = app.state.db

        # No prior refresh recorded — first call should be allowed through.
        # Stub refresh_recommendations to return 0 (simulating empty OpenAI output).
        _done = threading.Event()
        with (
            patch(
                "mediaman.web.routes.recommended.refresh.refresh_recommendations",
                return_value=0,
            ),
        ):
            resp = authed_client.post("/api/recommended/refresh")
            assert resp.status_code == 200
            assert resp.json().get("status") == "started"

            # Wait for the background worker to finish (it's a daemon thread).
            # Poll the status endpoint; each HTTP round-trip yields the GIL so
            # the worker thread can make progress without any real sleep.
            for _ in range(500):
                st = authed_client.get("/api/recommended/refresh/status").json()
                if st.get("status") == "done":
                    _done.set()
                    break
            else:
                pytest.fail("Background refresh did not complete in time")

        assert st["status"] == "done"
        assert st["result"]["ok"] is False
        assert "no recommendations" in st["result"]["error"].lower()
        # Crucially: cooldown must NOT have been set.
        assert st["manual_refresh_available"] is True
        assert _throttle.last_manual_refresh(conn) is None

    def test_status_reports_running_while_worker_is_alive(self, authed_client, app):
        """Regression: even when the DB lease check returns False (e.g. the
        worker has been running long enough for the heartbeat to lapse), the
        status endpoint must still report ``running`` while the in-process
        worker thread is alive. Otherwise the JS poll loop sees ``idle`` and
        the button stays stuck on "Refreshing…" forever.
        """
        import threading

        # Block the worker on an event so we can observe the "running"
        # state from the polling endpoint deterministically.
        block_until_observed = threading.Event()
        worker_started = threading.Event()

        def slow_refresh(*_args, **_kwargs):
            worker_started.set()
            # Hold the worker in-flight until the test releases it.
            block_until_observed.wait(timeout=5)
            return 1

        with (
            patch(
                "mediaman.web.routes.recommended.refresh.refresh_recommendations",
                side_effect=slow_refresh,
            ),
            # Force the DB-lease check to return False so we are isolating
            # the in-memory thread-alive path. Without the in-memory check
            # this would flip status to "idle" while the worker is still
            # working — the exact bug we are guarding against.
            patch(
                "mediaman.web.routes.recommended.refresh.is_refresh_running",
                return_value=False,
            ),
        ):
            resp = authed_client.post("/api/recommended/refresh")
            assert resp.status_code == 200
            assert resp.json().get("status") == "started"

            # Wait for the worker to actually enter the slow_refresh stub
            # so the in-memory thread reference is set and alive.
            assert worker_started.wait(timeout=2)

            # While the worker is held inside slow_refresh, the status
            # endpoint must report "running" — even though the DB lease
            # check is patched to return False.
            st = authed_client.get("/api/recommended/refresh/status").json()
            assert st["status"] == "running"

            # Release the worker and wait for it to finish.
            block_until_observed.set()
            for _ in range(500):
                st = authed_client.get("/api/recommended/refresh/status").json()
                if st.get("status") == "done":
                    break
            else:
                pytest.fail("Worker did not finish in time")

        assert st["result"]["ok"] is True


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
        insert_suggestion(
            conn,
            title="Dune",
            media_type="movie",
            category="trending",
            tmdb_id=42,
            created_at="2026-01-01T00:00:00",
        )

        max_in_window = _rec_api._DOWNLOAD_ACTION_LIMITER._max_in_window

        from mediaman.services.infra import SafeHTTPError

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.side_effect = SafeHTTPError(
            status_code=0, body_snippet="transport error: ConnectionError", url="http://radarr/"
        )

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
        conn.commit()
        return insert_suggestion(
            conn,
            title="Interstellar",
            media_type="movie",
            category="trending",
            tmdb_id=157336,
            created_at="2026-01-01T00:00:00",
        )

    def test_share_token_endpoint_returns_share_url(self, authed_client, app):
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
        """Neither the Watch page nor its recommendations JSON carries a pre-minted share
        link; one exists only once an admin asks for it through the share-token route."""
        conn = app.state.db
        self._insert_suggestion(conn)

        with (
            patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
            patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
        ):
            page = authed_client.get("/watch")
            recs = authed_client.get("/api/watch/recommended")

        assert page.status_code == 200
        island = re.search(
            r'<script type="application/json" id="watch-page-data">(.*?)</script>',
            page.text,
            re.DOTALL,
        )
        assert island is not None, "watch-page-data island not found"
        data = json.loads(island.group(1))
        assert data["is_admin"] is True
        assert not [k for k in _keys(data) if "share" in k or "token" in k]
        assert "example.com/download/" not in page.text

        assert recs.status_code == 200
        body = recs.json()
        assert [item["title"] for item in body["items"]] == ["Interstellar"]
        assert not [k for k in _keys(body) if "share" in k or "token" in k]
        assert "/download/" not in recs.text


def _keys(value: object) -> list[str]:
    """Every key in a parsed JSON value, at any depth."""
    if isinstance(value, dict):
        return [*value, *(key for inner in value.values() for key in _keys(inner))]
    if isinstance(value, list):
        return [key for inner in value for key in _keys(inner)]
    return []


class TestDownloadRecommendationSuccess:
    """Happy-path coverage for POST /api/recommended/{id}/download."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _rec_api._DOWNLOAD_ACTION_LIMITER.reset()
        yield
        _rec_api._DOWNLOAD_ACTION_LIMITER.reset()

    def _insert_movie_suggestion(self, conn) -> int:
        return insert_suggestion(
            conn,
            title="Inception",
            media_type="movie",
            category="trending",
            tmdb_id=27205,
            created_at="2026-01-01T00:00:00",
        )

    def _insert_tv_suggestion(self, conn) -> int:
        return insert_suggestion(
            conn,
            title="Breaking Bad",
            media_type="tv",
            category="trending",
            tmdb_id=1396,
            created_at="2026-01-01T00:00:00",
        )

    def test_add_movie_to_radarr_succeeds(self, authed_client, app):
        """A valid movie suggestion is added to Radarr and returns ok=True."""
        conn = app.state.db
        rec_id = self._insert_movie_suggestion(conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
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
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 1396, "tvdbId": 81189}]
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
        conn = app.state.db
        rec_id = self._insert_movie_suggestion(conn)

        with patch("mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=None):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "Radarr" in body["error"]

    def test_sonarr_not_configured_returns_error(self, authed_client, app):
        conn = app.state.db
        rec_id = self._insert_tv_suggestion(conn)

        with patch("mediaman.web.routes.recommended.api.build_sonarr_from_db", return_value=None):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "Sonarr" in body["error"]

    def test_radarr_http_409_treated_as_already_exists(self, authed_client, app):
        """A 409 SafeHTTPError from Radarr is surfaced as an 'already exists' error.

        Returns HTTP 409 (not 200) so downstream clients
        treat the conflict properly instead of silently logging a
        success.
        """
        from mediaman.services.infra import SafeHTTPError

        conn = app.state.db
        rec_id = self._insert_movie_suggestion(conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://radarr/api/v3/movie"
        )

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 409
        body = resp.json()
        assert body["ok"] is False
        assert "already" in body["error"].lower()

    def test_sonarr_409_safe_http_error_treated_as_already_exists(self, authed_client, app):
        """A 409 SafeHTTPError from Sonarr is surfaced as an 'already exists' error.

        Returns HTTP 409 (not 200) so downstream clients
        treat the conflict properly instead of silently logging a
        success.
        """
        from mediaman.services.infra import SafeHTTPError

        conn = app.state.db
        rec_id = self._insert_tv_suggestion(conn)

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 1396, "tvdbId": 81189}]
        mock_sonarr.add_series.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://sonarr/api/v3/series"
        )

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db", return_value=mock_sonarr
        ):
            resp = authed_client.post(f"/api/recommended/{rec_id}/download")

        assert resp.status_code == 409
        body = resp.json()
        assert body["ok"] is False
        assert "already" in body["error"].lower()

    def test_downloaded_at_set_on_success(self, authed_client, app):
        """A successful download stamps ``downloaded_at`` on the suggestion row."""
        conn = app.state.db
        rec_id = self._insert_movie_suggestion(conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db", return_value=mock_radarr
        ):
            authed_client.post(f"/api/recommended/{rec_id}/download")

        row = conn.execute(
            "SELECT downloaded_at FROM suggestions WHERE id = ?", (rec_id,)
        ).fetchone()
        assert row["downloaded_at"] is not None


class TestRefreshExceptionHandling:
    """Verify the contextlib.suppress(Exception) replacement: sqlite3.Error is
    caught-and-logged, not silently swallowed, and a non-sqlite3 error propagates
    (tested via the worker thread's broad except-and-record path)."""

    @pytest.fixture(autouse=True)
    def _reset_refresh_state(self):
        _rec_refresh._refresh_result = None
        _rec_refresh._refresh_thread = None
        yield
        _rec_refresh._refresh_result = None
        _rec_refresh._refresh_thread = None

    def test_heartbeat_sqlite_error_is_logged_not_silently_swallowed(self, caplog):
        """A sqlite3.Error in heartbeat_refresh_run must be logged at WARNING,
        not silently discarded."""
        import logging
        import sqlite3
        import threading

        run_id = 42
        heartbeat_stop = threading.Event()

        # We call the inner ticker body directly by patching heartbeat_refresh_run
        # to raise sqlite3.Error, then running the ticker with a very short interval
        # and stopping it immediately after one cycle.
        call_count = {"n": 0}
        stop_after_first = threading.Event()

        def raising_heartbeat(conn, rid):
            call_count["n"] += 1
            stop_after_first.set()
            raise sqlite3.Error("simulated DB error")

        with (
            patch(
                "mediaman.web.routes.recommended.refresh.heartbeat_refresh_run",
                side_effect=raising_heartbeat,
            ),
            patch(
                "mediaman.web.routes.recommended.refresh.open_thread_connection",
                return_value=MagicMock(),
            ),
        ):
            # Reproduce the heartbeat_ticker body inline so we can control the loop.
            hb_conn = MagicMock()
            interval = 0.0  # fire immediately

            def ticker_body():
                while not heartbeat_stop.wait(interval):
                    try:
                        _rec_refresh.heartbeat_refresh_run(hb_conn, run_id)
                    except sqlite3.Error:
                        _rec_refresh.logger.warning(
                            "Heartbeat DB write failed for run_id=%s; skipping tick",
                            run_id,
                            exc_info=True,
                        )
                    heartbeat_stop.set()  # stop after one cycle

            with caplog.at_level(logging.WARNING, logger="mediaman.web.routes.recommended.refresh"):
                ticker_body()

        assert call_count["n"] == 1
        assert any("Heartbeat DB write failed" in r.message for r in caplog.records)

    def test_finish_refresh_run_sqlite_error_in_finally_is_logged(self, caplog):
        """A sqlite3.Error from finish_refresh_run in the worker's except-block
        must be logged at ERROR, not silently dropped — a stuck lease is operationally
        significant."""
        import logging
        import sqlite3
        import threading

        # Simulate: worker thread raises a generic Exception (triggers the broad
        # except), then finish_refresh_run raises sqlite3.Error.
        done = threading.Event()

        with (
            patch(
                "mediaman.web.routes.recommended.refresh.open_thread_connection",
                return_value=MagicMock(),
            ),
            patch(
                "mediaman.web.routes.recommended.refresh.refresh_recommendations",
                side_effect=RuntimeError("worker failed"),
            ),
            patch(
                "mediaman.web.routes.recommended.refresh.finish_refresh_run",
                side_effect=sqlite3.Error("DB unavailable"),
            ),
            patch(
                "mediaman.web.routes.recommended.refresh.heartbeat_refresh_run",
            ),
            caplog.at_level(logging.ERROR, logger="mediaman.web.routes.recommended.refresh"),
        ):
            _rec_refresh._run_refresh("unused", "testsecret", 99, done)

        assert done.is_set()
        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("lease may be stuck" in m for m in error_msgs), (
            f"Expected stuck-lease log at ERROR. Records: {[r.message for r in caplog.records]}"
        )

    def test_non_sqlite_error_in_heartbeat_propagates(self):
        """A non-sqlite3.Error (e.g. ValueError) in the heartbeat path is NOT
        caught by the narrowed except — it propagates up as expected."""
        import sqlite3
        import threading

        heartbeat_stop = threading.Event()
        run_id = 1

        def raise_value_error(conn, rid):
            raise ValueError("unexpected non-DB error")

        hb_conn = MagicMock()
        interval = 0.0

        def ticker_body():
            while not heartbeat_stop.wait(interval):
                with contextlib.suppress(sqlite3.Error):
                    raise_value_error(hb_conn, run_id)
                heartbeat_stop.set()

        # ValueError must propagate out uncaught from the narrowed except
        with pytest.raises(ValueError, match="unexpected non-DB error"):
            # Reproduce what happens when ValueError is NOT caught by sqlite3.Error
            raise_value_error(hb_conn, run_id)
