"""Tests for the manual /api/recommended/refresh rate limit.

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
from mediaman.web.routes import recommended as rec


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
    return client


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """Each test starts with a clean global lock + result so that
    background-state from a prior test can't leak into this one."""
    rec._refresh_running = False
    rec._refresh_result = None
    yield
    rec._refresh_running = False
    rec._refresh_result = None


def _stamp_last_refresh(conn, when: datetime) -> None:
    """Pretend a manual refresh just ran at the given time."""
    rec._record_manual_refresh(conn, when)


# ---------------------------------------------------------------------------
# Cooldown helpers (pure functions — no FastAPI app needed)
# ---------------------------------------------------------------------------

class TestCooldownHelpers:
    def test_no_prior_refresh_means_no_cooldown(self, db_path):
        conn = init_db(str(db_path))
        try:
            assert rec._refresh_cooldown_remaining(conn) is None
        finally:
            conn.close()

    def test_recent_refresh_returns_remaining_time(self, db_path):
        conn = init_db(str(db_path))
        try:
            _stamp_last_refresh(conn, datetime.now(timezone.utc) - timedelta(hours=1))
            remaining = rec._refresh_cooldown_remaining(conn)
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
            assert rec._refresh_cooldown_remaining(conn) is None
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
            assert rec._refresh_cooldown_remaining(conn) is None
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
            "mediaman.services.arr_build.build_plex_from_db",
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
            "mediaman.services.arr_build.build_plex_from_db",
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

    def test_status_endpoint_reports_available_after_cooldown(
        self, authed_client, app
    ):
        conn = app.state.db
        _stamp_last_refresh(conn, datetime.now(timezone.utc) - timedelta(hours=25))
        resp = authed_client.get("/api/recommended/refresh/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["manual_refresh_available"] is True
        assert "cooldown_seconds" not in body
