"""Tests for subscriber admin API endpoints.

Covers: api_list_subscribers, api_add_subscriber, api_remove_subscriber,
and api_send_newsletter in mediaman.web.routes.subscribers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.db import init_db, set_connection
from mediaman.main import create_app


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
    conn = app.state.db
    create_user(conn, "testadmin", "testpass123", enforce_policy=False)
    token = create_session(conn, "testadmin")
    client = TestClient(app)
    client.cookies.set("session_token", token)
    return client


def _insert_subscriber(conn, email: str, active: int = 1) -> None:
    conn.execute(
        "INSERT INTO subscribers (email, active, created_at) VALUES (?, ?, ?)",
        (email, active, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# GET /api/subscribers
# ---------------------------------------------------------------------------


class TestListSubscribers:
    def test_returns_empty_list_when_no_subscribers(self, authed_client):
        resp = authed_client.get("/api/subscribers")
        assert resp.status_code == 200
        assert resp.json()["subscribers"] == []

    def test_returns_all_subscribers(self, authed_client, app):
        conn = app.state.db
        _insert_subscriber(conn, "alice@example.com")
        _insert_subscriber(conn, "bob@example.com", active=0)

        resp = authed_client.get("/api/subscribers")
        assert resp.status_code == 200
        subs = resp.json()["subscribers"]
        assert len(subs) == 2
        emails = {s["email"] for s in subs}
        assert "alice@example.com" in emails
        assert "bob@example.com" in emails

    def test_includes_active_flag(self, authed_client, app):
        conn = app.state.db
        _insert_subscriber(conn, "active@example.com", active=1)
        _insert_subscriber(conn, "inactive@example.com", active=0)

        resp = authed_client.get("/api/subscribers")
        subs = {s["email"]: s["active"] for s in resp.json()["subscribers"]}
        assert subs["active@example.com"] is True
        assert subs["inactive@example.com"] is False

    def test_unauthenticated_rejected(self, app):
        client = TestClient(app)
        resp = client.get("/api/subscribers", follow_redirects=False)
        assert resp.status_code in (302, 303, 401, 403)


# ---------------------------------------------------------------------------
# POST /api/subscribers
# ---------------------------------------------------------------------------


class TestAddSubscriber:
    def test_adds_valid_subscriber(self, authed_client, app):
        resp = authed_client.post("/api/subscribers", data={"email": "new@example.com"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert body["email"] == "new@example.com"

        # Verify it's actually in the DB
        conn = app.state.db
        row = conn.execute(
            "SELECT active FROM subscribers WHERE email = ?", ("new@example.com",)
        ).fetchone()
        assert row is not None
        assert row["active"] == 1

    def test_rejects_duplicate_email(self, authed_client, app):
        conn = app.state.db
        _insert_subscriber(conn, "dupe@example.com")

        resp = authed_client.post("/api/subscribers", data={"email": "dupe@example.com"})
        assert resp.status_code == 409

    def test_rejects_invalid_email_format(self, authed_client):
        resp = authed_client.post("/api/subscribers", data={"email": "not-an-email"})
        assert resp.status_code == 422

    def test_email_normalised_to_lowercase(self, authed_client, app):
        resp = authed_client.post("/api/subscribers", data={"email": "Upper@Example.COM"})
        assert resp.status_code == 201
        assert resp.json()["email"] == "upper@example.com"

    def test_unauthenticated_rejected(self, app):
        client = TestClient(app)
        resp = client.post(
            "/api/subscribers", data={"email": "x@example.com"}, follow_redirects=False
        )
        assert resp.status_code in (302, 303, 401, 403)


# ---------------------------------------------------------------------------
# DELETE /api/subscribers/{id}
# ---------------------------------------------------------------------------


class TestRemoveSubscriber:
    def test_removes_existing_subscriber(self, authed_client, app):
        conn = app.state.db
        _insert_subscriber(conn, "gone@example.com")
        row = conn.execute(
            "SELECT id FROM subscribers WHERE email = ?", ("gone@example.com",)
        ).fetchone()
        sub_id = row["id"]

        resp = authed_client.delete(f"/api/subscribers/{sub_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify it's gone
        row = conn.execute("SELECT id FROM subscribers WHERE id = ?", (sub_id,)).fetchone()
        assert row is None

    def test_returns_404_for_missing_id(self, authed_client):
        resp = authed_client.delete("/api/subscribers/99999")
        assert resp.status_code == 404

    def test_unauthenticated_rejected(self, app):
        client = TestClient(app)
        resp = client.delete("/api/subscribers/1", follow_redirects=False)
        assert resp.status_code in (302, 303, 401, 403)


# ---------------------------------------------------------------------------
# POST /api/newsletter/send
# ---------------------------------------------------------------------------


class TestSendNewsletter:
    @pytest.fixture(autouse=True)
    def _reset_newsletter_limiter(self):
        """Reset the newsletter rate-limiter between tests."""
        from mediaman.services.infra.rate_limits import NEWSLETTER_LIMITER

        NEWSLETTER_LIMITER._attempts.clear()
        NEWSLETTER_LIMITER._day_counts.clear()
        yield
        NEWSLETTER_LIMITER._attempts.clear()
        NEWSLETTER_LIMITER._day_counts.clear()

    def test_sends_to_active_subscribers(self, authed_client, app):
        conn = app.state.db
        _insert_subscriber(conn, "reader@example.com", active=1)

        with patch("mediaman.services.mail.newsletter.send_newsletter") as mock_send:
            mock_send.return_value = None
            resp = authed_client.post(
                "/api/newsletter/send",
                json={"recipients": ["reader@example.com"]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["sent_to"] == 1
        mock_send.assert_called_once()

    def test_rejects_empty_recipients(self, authed_client):
        resp = authed_client.post("/api/newsletter/send", json={"recipients": []})
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_rejects_unknown_recipients(self, authed_client, app):
        """Recipients not in the subscribers table must be silently dropped."""
        # Nobody in DB for this address
        with patch("mediaman.services.mail.newsletter.send_newsletter"):
            resp = authed_client.post(
                "/api/newsletter/send",
                json={"recipients": ["nobody@example.com"]},
            )

        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_rejects_inactive_subscriber(self, authed_client, app):
        conn = app.state.db
        _insert_subscriber(conn, "inactive@example.com", active=0)

        with patch("mediaman.services.mail.newsletter.send_newsletter"):
            resp = authed_client.post(
                "/api/newsletter/send",
                json={"recipients": ["inactive@example.com"]},
            )

        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_unauthenticated_rejected(self, app):
        client = TestClient(app)
        resp = client.post(
            "/api/newsletter/send",
            json={"recipients": ["x@example.com"]},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303, 401, 403)

    def test_send_failure_returns_502(self, authed_client, app):
        conn = app.state.db
        _insert_subscriber(conn, "ok@example.com", active=1)

        with patch(
            "mediaman.services.mail.newsletter.send_newsletter",
            side_effect=Exception("SMTP failure"),
        ):
            resp = authed_client.post(
                "/api/newsletter/send",
                json={"recipients": ["ok@example.com"]},
            )

        assert resp.status_code == 502
        assert resp.json()["ok"] is False
