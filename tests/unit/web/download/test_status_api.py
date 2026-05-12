"""Tests for the /api/download/status endpoint (legacy _make_download_app pattern).

Covers GET /api/download/status via the download router:
- new item-shape fields (state, progress, eta, episodes)
- invalid service → 422
- radarr ready (hasFile=True)
- radarr searching (no queue entry, no file)
- admin rate-limiting (must not bypass)
- timeleft HH:MM:SS → human-readable eta
- SafeHTTPError from Arr → unknown state (not 500)
- progress clamp to [0, 100]
- string-typed size fields do not crash
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.download import router as download_router


def _make_download_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(download_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _authed_client(db_path, secret_key):
    """Return an authenticated TestClient against the download router."""
    conn = init_db(str(db_path))
    app = _make_download_app(conn, secret_key)
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app)
    client.cookies.set("session_token", token)
    return client


class TestDownloadStatusAPI:
    @pytest.fixture(autouse=True)
    def _reset_caches(self):
        """Clear the per-(service, tmdb_id) status cache so each test sees
        fresh upstream calls instead of replaying a previous test's payload."""
        from mediaman.web.routes.download import reset_download_caches

        reset_download_caches()

    def test_status_returns_new_shape_fields(self, db_path, secret_key):
        """GET /api/download/status returns the new item shape with state field."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_queue_item = {
            "movie": {
                "title": "Dune",
                "tmdbId": 123,
                "images": [{"coverType": "poster", "remoteUrl": "http://img/poster.jpg"}],
            },
            "size": 6000000000,
            "sizeleft": 2000000000,
            "status": "downloading",
            "trackedDownloadStatus": "ok",
            "timeleft": "00:12:00",
        }
        mock_client = MagicMock()
        mock_client.get_queue.return_value = [mock_queue_item]
        mock_client.get_movie_by_tmdb.return_value = {"hasFile": False, "tmdbId": 123}

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=123")

        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert data["state"] == "downloading"
        assert "progress" in data
        assert "eta" in data
        assert "episodes" in data

    def test_status_invalid_service_returns_422(self, db_path, secret_key):
        """An empty / unknown ``service`` is rejected at the FastAPI layer.

        Previously the route silently fell through to an "unknown" item
        for any service string; the route now constrains ``service`` to
        ``Literal["radarr", "sonarr"]`` and ``tmdb_id`` to ``gt=0`` so
        malformed callers get a clear 422 rather than a placeholder
        success response."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        resp = client.get("/api/download/status?service=&tmdb_id=0")
        assert resp.status_code == 422

    def test_status_radarr_ready(self, db_path, secret_key):
        """Movie with hasFile=True returns state=ready with progress=100."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {
            "hasFile": True,
            "title": "Dune",
            "tmdbId": 42,
            "images": [],
        }

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ready"
        assert data["progress"] == 100

    def test_status_radarr_searching(self, db_path, secret_key):
        """Movie not in queue and no file → state=searching."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = None
        mock_client.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=999")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "searching"

    def test_admin_is_rate_limited(self, db_path, secret_key):
        """An admin session must NOT bypass the download-status rate
        limit. A stored XSS firing under an admin cookie (or a rogue
        admin) could otherwise hammer the endpoint uncapped."""
        from mediaman.web.routes import download as download_mod

        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        # Reset the module-level limiter so earlier tests don't
        # contaminate our bucket.
        download_mod._DOWNLOAD_STATUS_LIMITER._attempts.clear()

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {
            "hasFile": True,
            "title": "Dune",
            "tmdbId": 1,
            "images": [],
        }

        cap = download_mod._DOWNLOAD_STATUS_LIMITER._max_attempts

        # Burn through the whole window.
        try:
            with patch(
                "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
            ):
                for _ in range(cap):
                    r = client.get("/api/download/status?service=radarr&tmdb_id=1")
                    assert r.status_code == 200

                # Next admin call must be rejected — no bypass.
                r = client.get("/api/download/status?service=radarr&tmdb_id=1")

            assert r.status_code == 429
            assert r.json().get("error") == "too_many_requests"
        finally:
            # Leave the limiter clean so later tests aren't poisoned.
            download_mod._DOWNLOAD_STATUS_LIMITER._attempts.clear()

    def test_status_timeleft_formatting(self, db_path, secret_key):
        """timeleft HH:MM:SS is formatted as human-readable eta."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_queue_item = {
            "movie": {"title": "Test", "tmdbId": 77, "images": []},
            "size": 4000000000,
            "sizeleft": 1000000000,
            "status": "downloading",
            "trackedDownloadStatus": "downloading",
            "timeleft": "01:30:00",
        }
        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {"hasFile": False}
        mock_client.get_queue.return_value = [mock_queue_item]

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=77")

        assert resp.status_code == 200
        data = resp.json()
        assert "1 hr" in data["eta"]
        assert "remaining" in data["eta"]

    def test_status_safehttperror_returns_unknown_not_500(self, db_path, secret_key):
        """A SafeHTTPError from an Arr 5xx must surface as the 'unknown' state,
        not propagate as an unhandled exception → HTTP 500 to the client."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.side_effect = SafeHTTPError(
            status_code=500, body_snippet="boom", url="http://radarr.local"
        )

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=88")

        assert resp.status_code == 200
        assert resp.json()["state"] == "unknown"

    def test_status_progress_clamps_to_100(self, db_path, secret_key):
        """A misreported sizeleft larger than size must not produce a negative
        progress value or one above 100. Clamp to [0, 100]."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        # sizeleft (10 GB) > size (5 GB) → naive math yields -100% progress.
        # The clamp must coerce that to 0.
        mock_queue_item = {
            "movie": {"title": "Bad", "tmdbId": 33, "images": []},
            "size": 5_000_000_000,
            "sizeleft": 10_000_000_000,
            "status": "downloading",
            "trackedDownloadStatus": "downloading",
            "timeleft": "00:00:30",
        }
        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {"hasFile": False, "tmdbId": 33}
        mock_client.get_queue.return_value = [mock_queue_item]

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=33")

        assert resp.status_code == 200
        data = resp.json()
        assert 0 <= data["progress"] <= 100

    def test_status_string_size_does_not_crash(self, db_path, secret_key):
        """An Arr response with a stringified size must not raise TypeError —
        the safe-int coercion treats the field as zero and returns sensibly."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        # A malformed Arr response with size as a string (which used to make
        # ``size_total > 0`` raise TypeError) must round-trip cleanly.
        mock_queue_item = {
            "movie": {"title": "Test", "tmdbId": 55, "images": []},
            "size": "not-a-number",
            "sizeleft": "also-bad",
            "status": "downloading",
            "trackedDownloadStatus": "downloading",
            "timeleft": "00:01:00",
        }
        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {"hasFile": False, "tmdbId": 55}
        mock_client.get_queue.return_value = [mock_queue_item]

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=55")

        assert resp.status_code == 200
        data = resp.json()
        assert data["progress"] == 0
