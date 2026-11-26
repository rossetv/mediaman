"""Tests for mediaman.web.routes.download.status.

Covers GET /api/download/status:
  - unauthenticated (no token, no admin) → 401
  - authenticated via valid download token
  - authenticated via valid poll token
  - admin session bypasses token requirement
  - invalid/overlong tokens → 401
  - unknown service → unknown state item
  - _format_timeleft helper
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import generate_download_token, generate_poll_token, validate_poll_token
from mediaman.web.routes.download.status import (
    _DOWNLOAD_STATUS_LIMITER,
    _format_timeleft,
)
from mediaman.web.routes.download.status import (
    router as status_router,
)
from tests.helpers.factories import insert_recent_download


@pytest.fixture(autouse=True)
def _the_library_holds_every_title(monkeypatch):
    """A title its service has the file of is in the library here, to be played."""
    monkeypatch.setattr("mediaman.web.routes.download.status.library_title", lambda *a: "movie-1")


def _download_token(secret_key: str, media_type: str = "movie", tmdb_id: int = 42) -> str:
    return generate_download_token(
        email="test@example.com",
        action="download",
        title="Dune",
        media_type=media_type,
        tmdb_id=tmdb_id,
        recommendation_id=None,
        secret_key=secret_key,
    )


class TestFormatTimeleft:
    def test_empty_string_returns_empty(self):
        assert _format_timeleft("") == ""

    def test_none_like_empty_returns_empty(self):
        assert _format_timeleft("") == ""

    def test_invalid_format_returns_empty(self):
        assert _format_timeleft("not-a-time") == ""

    def test_hours_and_minutes(self):
        result = _format_timeleft("02:15:00")
        assert "2 hr" in result
        assert "15 min" in result

    def test_minutes_only(self):
        result = _format_timeleft("00:07:30")
        assert "7 min" in result
        assert "hr" not in result

    def test_seconds_only_returns_at_least_1_sec(self):
        result = _format_timeleft("00:00:45")
        assert "sec" in result
        assert "45" in result

    def test_zero_seconds_returns_1_sec(self):
        """Avoids "~0 sec remaining" — minimum is 1 sec."""
        result = _format_timeleft("00:00:00")
        assert "1 sec" in result

    def test_wrong_part_count_returns_empty(self):
        assert _format_timeleft("10:30") == ""

    def test_non_numeric_parts_returns_empty(self):
        assert _format_timeleft("aa:bb:cc") == ""


class TestDownloadStatusAuth:
    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _DOWNLOAD_STATUS_LIMITER.reset()

    def test_unauthenticated_returns_401(self, app_factory, conn):
        """No token, no admin session → 401."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/download/status?service=radarr&tmdb_id=42")
        assert resp.status_code == 401

    def test_download_token_no_longer_accepted(self, app_factory, conn, secret_key):
        """Download tokens are no longer accepted for status polling;
        only poll_token (short-lived) is valid for unauthenticated callers."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        token = _download_token(secret_key, media_type="movie", tmdb_id=42)

        # Download token passed as `token=` query param → 401 (param no longer accepted)
        resp = client.get(
            f"/api/download/status?service=radarr&tmdb_id=42&token={token}",
        )
        assert resp.status_code == 401

    def test_download_token_wrong_tmdb_id_rejected(self, app_factory, conn, secret_key):
        """A download token (now unsupported) still returns 401 regardless of tmdb_id match."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        token = _download_token(secret_key, media_type="movie", tmdb_id=99)  # wrong ID

        resp = client.get(
            f"/api/download/status?service=radarr&tmdb_id=42&token={token}",
        )
        assert resp.status_code == 401

    def test_valid_poll_token_authenticated(self, app_factory, conn, secret_key):
        """A valid poll token for the correct service and tmdb_id is accepted."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        poll_token = generate_poll_token(
            media_item_id="radarr:Dune",
            service="radarr",
            tmdb_id=42,
            secret_key=secret_key,
        )

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get(
                f"/api/download/status?service=radarr&tmdb_id=42&poll_token={poll_token}",
            )
        assert resp.status_code == 200

    def test_admin_session_bypasses_token_requirement(self, app_factory, authed_client, conn):
        """An authenticated admin can poll status without any download/poll token."""
        app = app_factory(status_router, conn=conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")
        assert resp.status_code == 200

    def test_overlong_token_returns_401(self, app_factory, conn):
        """A token over 4096 chars returns 401 without attempting validation."""
        app = app_factory(status_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)

        long_token = "x" * 4097
        resp = client.get(
            f"/api/download/status?service=radarr&tmdb_id=42&token={long_token}",
        )
        assert resp.status_code == 401

    def test_unknown_service_returns_422(self, app_factory, authed_client, conn):
        """An unrecognised service name is rejected at the type layer.

        The route signature is ``service: Literal["radarr", "sonarr"]``, so
        FastAPI's request validation returns 422 for any other value rather
        than reaching the handler and falling through to an 'unknown' state.
        """
        app = app_factory(status_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.get("/api/download/status?service=bogus&tmdb_id=42")
        assert resp.status_code == 422


class TestDownloadStatusRadarr:
    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _DOWNLOAD_STATUS_LIMITER.reset()

    def test_radarr_movie_with_file_returns_ready(self, app_factory, authed_client, conn):
        """When Radarr reports hasFile=True the status is ready."""
        app = app_factory(status_router, conn=conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = {
            "hasFile": True,
            "title": "Dune",
            "images": [],
        }
        mock_radarr.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ready"
        assert data["progress"] == 100

    def test_radarr_not_configured_returns_unknown(self, app_factory, authed_client, conn):
        """When Radarr is not configured (client=None) the status is unknown."""
        app = app_factory(status_router, conn=conn)
        client = authed_client(app, conn)

        with patch("mediaman.web.routes.download.status.build_radarr_from_db", return_value=None):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")

        assert resp.status_code == 200
        assert resp.json()["state"] == "unknown"


_STATUS_SECRET = "a" * 64


def _make_status_app_without_admin(conn) -> TestClient:
    """Stand up a status-router app with get_optional_admin always returning None."""

    from fastapi import FastAPI

    from mediaman.config import Config
    from mediaman.db import set_connection
    from mediaman.web.auth.middleware import get_optional_admin
    from mediaman.web.routes.download import status as status_mod

    app = FastAPI()
    app.include_router(status_mod.router)
    app.state.config = Config(secret_key=_STATUS_SECRET)
    app.state.db = conn
    set_connection(conn)
    app.dependency_overrides[get_optional_admin] = lambda: None
    return TestClient(app, raise_server_exceptions=True)


class TestPollTokenRequired:
    """Unauthenticated status polling must use poll_token, not download token."""

    def test_no_token_returns_401(self, conn):
        client = _make_status_app_without_admin(conn)
        resp = client.get("/api/download/status", params={"service": "radarr", "tmdb_id": 42})
        assert resp.status_code == 401

    def test_download_token_no_longer_accepted(self, conn):
        """The long-lived download token must not be accepted for polling."""
        download_token = generate_download_token(
            email="test@example.com",
            action="download",
            title="Test",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=None,
            secret_key=_STATUS_SECRET,
        )
        client = _make_status_app_without_admin(conn)
        # Passing the download token in the 'token' param must now be ignored
        # (the 'token' param was removed; the endpoint only knows poll_token).
        resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42, "token": download_token},
        )
        # Must not authenticate — 401 expected
        assert resp.status_code == 401

    def test_valid_poll_token_accepted(self, conn):
        """A valid poll_token bound to the correct service/tmdb must authenticate."""
        from mediaman.web.routes.download import status as status_mod

        poll_token = generate_poll_token(
            media_item_id="radarr:Test",
            service="radarr",
            tmdb_id=42,
            secret_key=_STATUS_SECRET,
        )
        client = _make_status_app_without_admin(conn)

        # Patch the service lookup so we don't need a real Radarr client
        with patch.object(
            status_mod,
            "_radarr_status",
            return_value={"state": "searching"},
        ):
            resp = client.get(
                "/api/download/status",
                params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
            )
        assert resp.status_code == 200

    def test_poll_token_wrong_service_rejected(self, conn):
        """A poll_token issued for 'sonarr' must not authenticate a 'radarr' request."""
        poll_token = generate_poll_token(
            media_item_id="sonarr:Test",
            service="sonarr",
            tmdb_id=42,
            secret_key=_STATUS_SECRET,
        )
        client = _make_status_app_without_admin(conn)
        resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
        )
        assert resp.status_code == 401


class TestPollTokenRefresh:
    """A page left open on a long download keeps a working poll token."""

    @pytest.fixture(autouse=True)
    def _isolate(self):
        from mediaman.web.routes.download import reset_download_caches

        _DOWNLOAD_STATUS_LIMITER.reset()
        reset_download_caches()

    def _poll(self, conn, poll_token: str, state: str = "downloading"):
        from mediaman.web.routes.download import status as status_mod

        client = _make_status_app_without_admin(conn)
        with patch.object(status_mod, "_radarr_status", return_value={"state": state}):
            return client.get(
                "/api/download/status",
                params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
            )

    def _token(self, ttl_seconds: int, first_issued: int | None = None) -> str:
        return generate_poll_token(
            media_item_id="radarr:Test",
            service="radarr",
            tmdb_id=42,
            secret_key=_STATUS_SECRET,
            ttl_seconds=ttl_seconds,
            first_issued=first_issued,
        )

    def _old_style_token(self, ttl_seconds: int) -> str:
        from mediaman.crypto import tokens

        return tokens._encode_signed(
            {
                "mid": "radarr:Test",
                "svc": "radarr",
                "tmdb": 42,
                "nonce": "00",
                "exp": int(time.time()) + ttl_seconds,
            },
            _STATUS_SECRET,
            tokens._TOKEN_PURPOSE_POLL,
        )

    def test_token_near_expiry_gets_a_fresh_valid_replacement(self, conn):
        old = self._token(ttl_seconds=60)
        resp = self._poll(conn, old)

        assert resp.status_code == 200
        fresh = resp.json()["poll_token"]
        payload = validate_poll_token(fresh, _STATUS_SECRET)
        assert payload is not None
        assert payload["svc"] == "radarr"
        assert payload["tmdb"] == 42
        assert payload["exp"] > validate_poll_token(old, _STATUS_SECRET)["exp"]
        assert self._poll(conn, fresh).status_code == 200

    def test_token_near_expiry_is_not_renewed_once_download_is_ready(self, conn):
        resp = self._poll(conn, self._token(ttl_seconds=60), state="ready")

        assert resp.status_code == 200
        assert resp.json()["state"] == "ready"
        assert "poll_token" not in resp.json()

    def test_token_near_expiry_is_renewed_while_status_is_unknown(self, conn):
        resp = self._poll(conn, self._token(ttl_seconds=60), state="unknown")

        assert "poll_token" in resp.json()

    def test_token_with_most_of_its_life_left_is_not_replaced(self, conn):
        resp = self._poll(conn, self._token(ttl_seconds=600))

        assert resp.status_code == 200
        assert "poll_token" not in resp.json()

    def test_expired_token_is_refused(self, conn):
        resp = self._poll(conn, self._token(ttl_seconds=-1))

        assert resp.status_code == 401
        assert "poll_token" not in resp.json()

    def test_token_first_issued_over_a_day_ago_is_not_renewed(self, conn):
        first = int(time.time()) - 86400 - 60
        resp = self._poll(conn, self._token(ttl_seconds=60, first_issued=first))

        assert resp.status_code == 200
        assert "poll_token" not in resp.json()

    def test_renewal_carries_the_first_issue_time_forward(self, conn):
        first = int(time.time()) - 3600
        resp = self._poll(conn, self._token(ttl_seconds=60, first_issued=first))

        payload = validate_poll_token(resp.json()["poll_token"], _STATUS_SECRET)
        assert payload is not None
        assert payload["first"] == first

    def test_token_without_a_first_issue_time_still_works_and_renews(self, conn):
        old = self._old_style_token(ttl_seconds=60)
        assert validate_poll_token(old, _STATUS_SECRET) is not None

        resp = self._poll(conn, old)

        assert resp.status_code == 200
        payload = validate_poll_token(resp.json()["poll_token"], _STATUS_SECRET)
        assert payload is not None
        # Its life began 600 seconds before it was due to expire.
        assert payload["first"] == pytest.approx(time.time() - 540, abs=5)


class TestStatusTellsWhatTheArrHolds:
    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _DOWNLOAD_STATUS_LIMITER.reset()

    def _status(self, app_factory, authed_client, conn, service, arr):
        client = authed_client(app_factory(status_router, conn=conn), conn)
        with patch(
            f"mediaman.web.routes.download.status.build_{service}_from_db", return_value=arr
        ):
            return client.get(f"/api/download/status?service={service}&tmdb_id=42").json()

    @pytest.mark.parametrize("service", ["radarr", "sonarr"])
    def test_a_title_with_no_file_is_not_ready_for_a_download_of_its_name_days_ago(
        self, app_factory, authed_client, conn, service
    ):
        insert_recent_download(conn, dl_id=f"{service}:Dune", title="Dune")
        arr = MagicMock()
        arr.get_queue.return_value = []
        arr.get_movie_by_tmdb.return_value = {"tmdbId": 42, "title": "Dune", "hasFile": False}
        arr.get_series.return_value = [{"tmdbId": 42, "title": "Dune", "statistics": {}}]

        data = self._status(app_factory, authed_client, conn, service, arr)

        assert data["state"] == "searching"

    @pytest.mark.parametrize(("files", "state"), [(9, "searching"), (10, "ready")])
    def test_a_show_is_ready_only_once_every_aired_season_is_all_there(
        self, app_factory, authed_client, conn, files, state
    ):
        seasons = [
            {
                "seasonNumber": number,
                "monitored": True,
                "statistics": {
                    "episodeCount": 10,
                    "episodeFileCount": count,
                    "previousAiring": "2020-01-01T00:00:00Z",
                },
            }
            for number, count in [(1, 10), (2, files)]
        ]
        sonarr = MagicMock()
        sonarr.get_queue.return_value = []
        sonarr.get_series.return_value = [
            {
                "tmdbId": 42,
                "title": "Show",
                "statistics": {"episodeFileCount": 10 + files},
                "seasons": seasons,
            }
        ]

        data = self._status(app_factory, authed_client, conn, "sonarr", sonarr)

        assert data["state"] == state

    def test_a_season_pack_is_counted_once_and_single_episodes_each(
        self, app_factory, authed_client, conn
    ):
        gib = 2**30
        queue = [
            {
                "series": {"tmdbId": 42, "title": "Show"},
                "episode": {"seasonNumber": 1, "episodeNumber": n},
                "size": size * gib,
                "sizeleft": size * gib // 2,
                "downloadId": download_id,
                "status": "downloading",
            }
            for n, size, download_id in [(1, 10, "PACK"), (2, 10, "PACK"), (3, 4, ""), (4, 4, "")]
        ]
        sonarr = MagicMock()
        sonarr.get_queue.return_value = queue

        data = self._status(app_factory, authed_client, conn, "sonarr", sonarr)

        assert (data["size_done"], data["size_total"]) == ("9.0 GB", "18.0 GB")
        assert len(data["episodes"]) == 4
