"""Tests for subscriber admin API endpoints.

Covers: api_list_subscribers, api_add_subscriber, api_remove_subscriber,
and api_send_newsletter in mediaman.web.routes.subscribers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session


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
    client.headers.update({"Origin": "http://testserver"})
    return client


@pytest.fixture(autouse=True)
def _reset_subscriber_limiter():
    """Reset the per-admin subscriber limiter so suite ordering does not
    cause the second / third test in a class to hit the daily cap."""
    from mediaman.services.rate_limit.instances import SUBSCRIBER_WRITE_LIMITER

    SUBSCRIBER_WRITE_LIMITER.reset()
    yield
    SUBSCRIBER_WRITE_LIMITER.reset()


def _insert_subscriber(conn, email: str, active: int = 1) -> None:
    conn.execute(
        "INSERT INTO subscribers (email, active, created_at) VALUES (?, ?, ?)",
        (email, active, datetime.now(UTC).isoformat()),
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
        from mediaman.services.rate_limit.instances import NEWSLETTER_LIMITER

        NEWSLETTER_LIMITER.reset()
        yield
        NEWSLETTER_LIMITER.reset()

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


# ---------------------------------------------------------------------------
# Audit logging + rate limiting (Domain 03 findings 9-11, 14)
# ---------------------------------------------------------------------------


class TestSubscriberAuditEvents:
    """Add, remove, and newsletter sends must each write a security_event
    row — without them, a compromised admin token can churn the
    subscriber list and dispatch newsletters with no audit trail. Per
    CODE_GUIDELINES §7.5/§10.10 the audit log records full actor/target
    identity (including email); masking belongs on operational logs only."""

    def test_add_writes_security_event(self, authed_client, app):
        conn = app.state.db
        resp = authed_client.post("/api/subscribers", data={"email": "audit@example.com"})
        assert resp.status_code == 201

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:subscriber.added'"
        ).fetchall()
        assert rows, "audit row missing for sec:subscriber.added"
        # Audit log records the target of the action — full email is required.
        assert "audit@example.com" in rows[0]["detail"]

    def test_remove_writes_security_event(self, authed_client, app):
        conn = app.state.db
        _insert_subscriber(conn, "rmv@example.com")
        sub_id = conn.execute(
            "SELECT id FROM subscribers WHERE email = ?", ("rmv@example.com",)
        ).fetchone()["id"]

        resp = authed_client.delete(f"/api/subscribers/{sub_id}")
        assert resp.status_code == 200

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:subscriber.removed'"
        ).fetchall()
        assert rows, "audit row missing for sec:subscriber.removed"
        assert "rmv@example.com" in rows[0]["detail"]

    def test_newsletter_send_writes_security_event(self, authed_client, app):
        from mediaman.services.rate_limit.instances import NEWSLETTER_LIMITER

        NEWSLETTER_LIMITER.reset()

        conn = app.state.db
        _insert_subscriber(conn, "letter@example.com", active=1)
        with patch("mediaman.services.mail.newsletter.send_newsletter"):
            resp = authed_client.post(
                "/api/newsletter/send",
                json={"recipients": ["letter@example.com"]},
            )
        assert resp.status_code == 200

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:newsletter.sent'"
        ).fetchall()
        assert rows, "audit row missing for sec:newsletter.sent"


class TestSubscriberAddRemoveRateLimit:
    """Add/remove must be rate-limited per admin so a leaked session
    cookie cannot script thousands of operations."""

    def test_add_throttled_after_burst(self, authed_client, app):
        # Limiter is 5/min — sixth call must hit 429.
        for i in range(5):
            resp = authed_client.post("/api/subscribers", data={"email": f"u{i}@example.com"})
            assert resp.status_code == 201
        resp = authed_client.post("/api/subscribers", data={"email": "extra@example.com"})
        assert resp.status_code == 429


class TestSubscriberAddLogging:
    """The info log on a successful add records the full email so the
    operator can correlate against the subscriber list during triage —
    see CODE_GUIDELINES §7.4."""

    def test_log_message_contains_full_email(self, authed_client, app, caplog):
        with caplog.at_level("INFO"):
            resp = authed_client.post("/api/subscribers", data={"email": "secret@example.com"})
        assert resp.status_code == 201
        joined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "secret@example.com" in joined


class TestSubscriberAddRace:
    """Two concurrent admins adding the same email — one wins, the other
    sees a clean 409 instead of a 500 from IntegrityError."""

    def test_duplicate_via_integrity_error_returns_409(self, authed_client, app):
        # Insert directly so the in-test admin session does not consume
        # rate-limit budget.
        conn = app.state.db
        _insert_subscriber(conn, "race@example.com")

        # POST will SELECT first and find the row — we should get 409
        # via the explicit existence check.
        resp = authed_client.post("/api/subscribers", data={"email": "race@example.com"})
        assert resp.status_code == 409
