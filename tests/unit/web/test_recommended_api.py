"""Tests for `mediaman.web.routes.recommended.api`.

Covers share-token minting and related guards.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import validate_download_token
from mediaman.db import set_connection
from mediaman.services.infra.http import SafeHTTPError
from mediaman.web.auth.middleware import get_current_admin, get_current_user
from mediaman.web.auth.password_hash import create_user, set_user_email
from mediaman.web.routes.recommended.api import (
    reset_download_action_limiter,
    reset_share_token_limiter,
)
from mediaman.web.routes.recommended.api import (
    router as rec_router,
)
from tests.helpers.factories import insert_suggestion

_SECRET = "a" * 64


def _make_rec_app(conn) -> TestClient:
    """Stand up a recommended-api app with admin always authenticated."""
    app = FastAPI()
    app.include_router(rec_router)
    app.state.config = Config(secret_key=_SECRET)
    app.state.db = conn
    set_connection(conn)
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_current_user] = lambda: "admin"
    return TestClient(app, raise_server_exceptions=True)


class TestMintRequiresTmdbId:
    """Share-token mint must refuse recommendations without a TMDB id."""

    def test_mint_without_tmdb_id_returns_422(self, conn):
        """Minting a share token for a suggestion without tmdb_id must return 422."""
        sid = insert_suggestion(conn, title="Test Movie", tmdb_id=None)
        client = _make_rec_app(conn)
        resp = client.post(f"/api/recommended/{sid}/share-token")
        assert resp.status_code == 422
        body = resp.json()
        assert not body.get("ok")
        assert "TMDB" in body.get("error", "")
        assert body["message"] == body["error"]

    def test_mint_with_tmdb_id_would_succeed_if_base_url_set(self, conn):
        """Minting with a tmdb_id present does not fail on the identifier check."""
        sid = insert_suggestion(conn, title="Test Movie", tmdb_id=12345)
        client = _make_rec_app(conn)
        resp = client.post(f"/api/recommended/{sid}/share-token")
        # Should NOT return 422; may return 200 or other error (e.g. missing base_url)
        assert resp.status_code != 422


class TestNewsletterSkipsMintWithoutTmdb:
    """Newsletter must skip redownload mint when no tmdb_id.

    The previous code hardcoded ``tmdb_id=None`` for deleted items, producing
    a public token whose submit fell back to ``lookup_by_term(title)``.  The
    fix is to omit the redownload URL entirely when the deleted item carries
    no stable identifier.  The template hides the button via
    ``{% if item.redownload_url %}``.
    """

    def test_deleted_item_without_tmdb_has_empty_redownload_url(self):
        from mediaman.services.mail.newsletter.subscribers import (
            _send_to_subscribers as _send_to_recipients,
        )

        captured: dict = {}

        class _FakeTemplate:
            def render(self, **kwargs):
                captured.update(kwargs)
                return "<html></html>"

        mailgun_stub = MagicMock()
        mailgun_stub.send_message.return_value = True

        deleted_no_tmdb = [{"title": "Ambiguous Title", "media_type": "movie"}]
        deleted_with_tmdb = [{"title": "Specific Film", "media_type": "movie", "tmdb_id": 12345}]

        _send_to_recipients(
            recipient_emails=["dest@example.com"],
            scheduled_items=[],
            deleted_items=deleted_no_tmdb + deleted_with_tmdb,
            this_week_items=[],
            storage={"total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "by_type": {}},
            reclaimed_week=0,
            reclaimed_month=0,
            reclaimed_total=0,
            subject="x",
            base_url="https://mm.example.com",
            secret_key=_SECRET,
            dry_run=False,
            template=_FakeTemplate(),
            mailgun=mailgun_stub,
            report_date="2026-05-01",
            conn=None,
        )

        rendered_deleted = captured["deleted_items"]
        assert len(rendered_deleted) == 2
        # No tmdb_id → no redownload link, regardless of base_url.
        assert rendered_deleted[0]["redownload_url"] == ""
        # With tmdb_id → token-bearing URL.
        assert rendered_deleted[1]["redownload_url"].startswith("https://mm.example.com/download/")


class TestDownloadRecommendationAdminEmail:
    """``api_download_recommendation`` must resolve the admin's email before
    recording a download notification, and skip the notification entirely
    when the admin has no email set — an admin email is never mandatory.
    """

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        reset_download_action_limiter()

    def test_movie_records_notification_with_resolved_admin_email(self, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        set_user_email(conn, "admin", "admin@example.com")
        sid = insert_suggestion(conn, title="Inception", tmdb_id=27205, media_type="movie")
        client = _make_rec_app(conn)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db",
            return_value=mock_radarr,
        ):
            resp = client.post(f"/api/recommended/{sid}/download")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = conn.execute(
            "SELECT email FROM download_notifications WHERE tmdb_id = 27205"
        ).fetchone()
        assert row is not None
        assert row["email"] == "admin@example.com"

    def test_movie_skips_notification_when_admin_has_no_email(self, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        # No set_user_email — the admin's email is NULL.
        sid = insert_suggestion(conn, title="Inception", tmdb_id=27205, media_type="movie")
        client = _make_rec_app(conn)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db",
            return_value=mock_radarr,
        ):
            resp = client.post(f"/api/recommended/{sid}/download")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # The movie is still added to Radarr — only the notification is skipped.
        mock_radarr.add_movie.assert_called_once()
        count = conn.execute("SELECT COUNT(*) FROM download_notifications").fetchone()[0]
        assert count == 0

    def test_tv_records_notification_with_resolved_admin_email(self, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        set_user_email(conn, "admin", "admin@example.com")
        sid = insert_suggestion(conn, title="Severance", tmdb_id=136315, media_type="tv")
        client = _make_rec_app(conn)
        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 136315, "tvdbId": 999}]
        mock_sonarr.add_series.return_value = None

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db",
            return_value=mock_sonarr,
        ):
            resp = client.post(f"/api/recommended/{sid}/download")

        assert resp.status_code == 200
        row = conn.execute(
            "SELECT email FROM download_notifications WHERE tmdb_id = 136315"
        ).fetchone()
        assert row is not None
        assert row["email"] == "admin@example.com"

    def test_tv_skips_notification_when_admin_has_no_email(self, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        sid = insert_suggestion(conn, title="Severance", tmdb_id=136315, media_type="tv")
        client = _make_rec_app(conn)
        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 136315, "tvdbId": 999}]
        mock_sonarr.add_series.return_value = None

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db",
            return_value=mock_sonarr,
        ):
            resp = client.post(f"/api/recommended/{sid}/download")

        assert resp.status_code == 200
        mock_sonarr.add_series.assert_called_once()
        count = conn.execute("SELECT COUNT(*) FROM download_notifications").fetchone()[0]
        assert count == 0


class TestShareTokenCarriesNoEmail:
    """A share link goes to a friend, who can decode the token payload, so
    the minted token must never carry the admin's email address.
    """

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        reset_share_token_limiter()

    @staticmethod
    def _set_base_url(conn) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('base_url', 'https://mm.example.com', 0, datetime('now'))"
        )
        conn.commit()

    def test_token_payload_has_no_email_when_the_admin_has_one(self, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        set_user_email(conn, "admin", "admin@example.com")
        self._set_base_url(conn)
        sid = insert_suggestion(conn, title="Inception", tmdb_id=27205, media_type="movie")
        client = _make_rec_app(conn)

        resp = client.post(f"/api/recommended/{sid}/share-token")

        assert resp.status_code == 200
        payload = validate_download_token(resp.json()["token"], _SECRET)
        assert payload is not None
        assert payload["email"] is None
        assert payload["tmdb"] == 27205


class TestDownloadRecommendationAlreadyThere:
    """An Arr answering 409 says the title is already in the Arr, not on a page."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        reset_download_action_limiter()

    def test_movie_409_names_radarr(self, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        sid = insert_suggestion(conn, title="Inception", tmdb_id=27205, media_type="movie")
        client = _make_rec_app(conn)
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None
        mock_radarr.add_movie.side_effect = SafeHTTPError(409, "exists", "radarr/api/v3/movie")

        with patch(
            "mediaman.web.routes.recommended.api.build_radarr_from_db",
            return_value=mock_radarr,
        ):
            resp = client.post(f"/api/recommended/{sid}/download")

        assert resp.status_code == 409
        assert resp.json()["error"] == "'Inception' is already in Radarr"

    def test_tv_409_names_sonarr(self, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        sid = insert_suggestion(conn, title="Severance", tmdb_id=136315, media_type="tv")
        client = _make_rec_app(conn)
        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 136315, "tvdbId": 999}]
        mock_sonarr.add_series.side_effect = SafeHTTPError(409, "exists", "sonarr/api/v3/series")

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db",
            return_value=mock_sonarr,
        ):
            resp = client.post(f"/api/recommended/{sid}/download")

        assert resp.status_code == 409
        assert resp.json()["error"] == "'Severance' is already tracked by Sonarr — manage it there"


class TestDownloadRecommendationAsksTheArrFirst:
    """Radarr and Sonarr answer 400, not 409, for a title they hold."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        reset_download_action_limiter()

    @staticmethod
    def _post(conn, kind, tmdb_id, arr):
        if not conn.execute("SELECT 1 FROM admin_users WHERE username='admin'").fetchone():
            create_user(conn, "admin", "password1234", enforce_policy=False)
        title = "Inception" if kind == "movie" else "Severance"
        sid = insert_suggestion(conn, title=title, tmdb_id=tmdb_id, media_type=kind)
        builder = "build_radarr_from_db" if kind == "movie" else "build_sonarr_from_db"
        with patch(f"mediaman.web.routes.recommended.api.{builder}", return_value=arr):
            return _make_rec_app(conn).post(f"/api/recommended/{sid}/download")

    def test_a_movie_radarr_holds_is_already_there_without_an_add(self, conn):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 4, "monitored": True}

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 409
        assert resp.json()["error"] == "'Inception' is already in Radarr"
        radarr.add_movie.assert_not_called()

    def test_a_series_sonarr_tracks_is_already_there_without_an_add(self, conn):
        sonarr = MagicMock()
        sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 136315, "tvdbId": 999}]
        sonarr.get_series.return_value = [{"tvdbId": 999}]

        resp = self._post(conn, "tv", 136315, sonarr)

        assert resp.status_code == 409
        assert resp.json()["error"] == "'Severance' is already tracked by Sonarr — manage it there"
        sonarr.add_series.assert_not_called()

    def test_a_movie_add_that_races_with_a_400_ends_as_already_there(self, conn):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.side_effect = [None, {"id": 4, "monitored": True}]
        radarr.add_movie.side_effect = SafeHTTPError(400, "has already been added", "radarr/m")

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 409
        assert resp.json()["error"] == "'Inception' is already in Radarr"

    def test_a_series_add_that_races_with_a_400_ends_as_already_there(self, conn):
        sonarr = MagicMock()
        sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 136315, "tvdbId": 999}]
        sonarr.get_series.side_effect = [[], [{"tvdbId": 999}]]
        sonarr.add_series.side_effect = SafeHTTPError(400, "has already been added", "sonarr/s")

        resp = self._post(conn, "tv", 136315, sonarr)

        assert resp.status_code == 409

    def test_a_400_for_a_title_the_arr_does_not_hold_stays_a_failure(self, conn):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None
        radarr.add_movie.side_effect = SafeHTTPError(400, "Invalid root folder", "radarr/m")

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 502
        assert resp.json()["error"] == "Failed to add to download queue"

    def test_a_movie_radarr_holds_unmonitored_is_remonitored_like_search(self, conn):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 4, "monitored": False}
        create_user(conn, "admin", "password1234", enforce_policy=False)
        set_user_email(conn, "admin", "admin@example.com")

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "message": "Added 'Inception' to Radarr"}
        radarr.remonitor_movie.assert_called_once_with(4)
        radarr.add_movie.assert_not_called()
        row = conn.execute("SELECT downloaded_at FROM suggestions").fetchone()
        assert row["downloaded_at"] is not None
        audit = conn.execute("SELECT detail FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert audit["detail"] == "Added 'Inception' to Radarr by admin"
        note = conn.execute("SELECT email, service FROM download_notifications").fetchone()
        assert (note["email"], note["service"]) == ("admin@example.com", "radarr")

    def test_an_unmonitored_movie_with_no_usable_id_is_a_failure_not_a_dead_end_409(self, conn):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": None, "monitored": False}

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 502
        radarr.remonitor_movie.assert_not_called()

    @pytest.mark.parametrize("status", [400, 409, 422])
    def test_a_failed_remonitor_ends_as_a_failure_and_records_nothing(self, conn, status):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 4, "monitored": False}
        radarr.remonitor_movie.side_effect = SafeHTTPError(status, "refused", "radarr/m")

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 502
        assert resp.json()["error"] == "Failed to add to download queue"
        assert conn.execute("SELECT downloaded_at FROM suggestions").fetchone()[0] is None
        radarr.unmonitor_movie.assert_called_once_with(4, max_retries=1)
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0

    @pytest.mark.parametrize("status", [409, 422])
    def test_a_refused_read_of_the_arr_is_not_read_as_already_there(self, conn, status):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.side_effect = SafeHTTPError(status, "refused", "radarr/m")

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 502

    def test_a_refused_read_of_sonarr_is_not_read_as_already_there(self, conn):
        sonarr = MagicMock()
        sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 136315, "tvdbId": 999}]
        sonarr.get_series.side_effect = SafeHTTPError(409, "refused", "sonarr/s")

        resp = self._post(conn, "tv", 136315, sonarr)

        assert resp.status_code == 502

    @pytest.mark.parametrize("kind", ["movie", "tv"])
    def test_an_add_with_no_answer_that_landed_is_a_success_and_mails_the_person(self, conn, kind):
        arr = MagicMock()
        arr.get_movie_by_tmdb.side_effect = [None, {"id": 4, "monitored": True}]
        arr.lookup_by_tmdb_id.return_value = [{"tmdbId": 136315, "tvdbId": 999}]
        arr.get_series.side_effect = [[], [{"tvdbId": 999, "monitored": True}]]
        arr.add_movie.side_effect = arr.add_series.side_effect = SafeHTTPError(0, "timeout", "a/b")
        create_user(conn, "admin", "password1234", enforce_policy=False)
        set_user_email(conn, "admin", "admin@example.com")

        resp = self._post(conn, kind, 136315, arr)

        assert resp.status_code == 200
        assert conn.execute("SELECT downloaded_at FROM suggestions").fetchone()[0] is not None
        note = conn.execute("SELECT email FROM download_notifications").fetchone()
        assert note["email"] == "admin@example.com"

    def test_an_add_with_no_answer_that_did_not_land_is_a_failure(self, conn):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None
        radarr.add_movie.side_effect = SafeHTTPError(0, "timeout", "a/b")

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 502
        assert conn.execute("SELECT downloaded_at FROM suggestions").fetchone()[0] is None

    def test_a_refused_connection_is_a_failure_answer_not_a_crash(self, conn):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.side_effect = requests.ConnectionError("refused")

        resp = self._post(conn, "movie", 27205, radarr)

        assert resp.status_code == 502
