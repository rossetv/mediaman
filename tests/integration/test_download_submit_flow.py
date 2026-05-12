"""Integration: mint token → POST /download/{token} → audit_log + notifications.

Covers the happy path (movie via fake Radarr) and the 409 'already exists'
path, plus the replay-protection seam. Exercises:

  crypto.generate_download_token  →  download.submit route
  →  audit_log table
  →  download_notifications table
  →  poll_token in response
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import generate_download_token, validate_poll_token
from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.download import (
    reset_download_caches,
    reset_download_limiters,
    reset_used_tokens,
)
from mediaman.web.routes.download.submit import router as submit_router


def _clear_state():
    """Reset all download-route shared state via the public reset API.

    Wave 4 added :func:`reset_download_limiters`, :func:`reset_download_caches`
    and :func:`reset_used_tokens` precisely so tests don't have to reach
    into module privates. Keep this helper aligned with that surface.
    """
    reset_used_tokens()
    reset_download_limiters()
    reset_download_caches()


def _make_movie_token(secret_key: str, title: str = "Alien: Romulus", tmdb_id: int = 945961) -> str:
    return generate_download_token(
        email="fan@example.com",
        action="download",
        title=title,
        media_type="movie",
        tmdb_id=tmdb_id,
        recommendation_id=None,
        secret_key=secret_key,
    )


class TestDownloadSubmitHappyPath:
    @pytest.fixture(autouse=True)
    def _reset_state(self):
        _clear_state()

    def test_movie_download_writes_audit_and_notification(self, app_factory, conn, secret_key):
        """POST /download/{token} with a fake Radarr writes audit_log + download_notifications."""
        app = app_factory(submit_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_movie_token(secret_key)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "poll_token" in body

        # Validate the poll token is well-formed (crosses crypto module).
        poll_payload = validate_poll_token(body["poll_token"], secret_key)
        assert poll_payload is not None
        assert poll_payload["svc"] == "radarr"

        # Assert audit_log row exists.
        audit = conn.execute("SELECT action FROM audit_log WHERE action='downloaded'").fetchone()
        assert audit is not None

        # Assert download_notifications row exists.
        notif = conn.execute(
            "SELECT email, title FROM download_notifications WHERE email='fan@example.com'"
        ).fetchone()
        assert notif is not None
        assert notif["title"] == "Alien: Romulus"


class TestDownloadSubmitReplay:
    @pytest.fixture(autouse=True)
    def _reset_state(self):
        _clear_state()

    def test_token_replay_returns_409(self, app_factory, conn, secret_key):
        """Using the same token twice returns 409 on the second attempt."""
        app = app_factory(submit_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_movie_token(secret_key)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            r1 = client.post(f"/download/{token}")
            r2 = client.post(f"/download/{token}")

        assert r1.status_code == 200
        assert r2.status_code == 409
        assert "already been used" in r2.json()["error"]

        # Only one notification row despite two requests.
        count = conn.execute("SELECT COUNT(*) FROM download_notifications").fetchone()[0]
        assert count == 1


class TestDownloadSubmitAlreadyExists:
    @pytest.fixture(autouse=True)
    def _reset_state(self):
        _clear_state()

    def test_arr_409_returns_409_with_poll_token(self, app_factory, conn, secret_key):
        """When Radarr responds 409 (already exists), route returns 409 with poll_token."""
        app = app_factory(submit_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        token = _make_movie_token(secret_key)

        mock_radarr = MagicMock()
        mock_radarr.add_movie.side_effect = SafeHTTPError(409, "Conflict", "http://radarr.local")

        with patch(
            "mediaman.web.routes.download.submit.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(f"/download/{token}")

        assert resp.status_code == 409
        body = resp.json()
        assert body["ok"] is False
        assert "already exists" in body["error"]
        assert "poll_token" in body

        # Nothing written to audit_log or notifications on a conflict.
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        assert audit_count == 0
