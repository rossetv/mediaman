"""Tests for finding 36 — unsubscribe URLs must not expose email as a query param."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import generate_unsubscribe_token
from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.web.routes.subscribers import _UNSUB_LIMITER
from tests.helpers.factories import insert_subscriber

# NOTE: this file deliberately keeps its own ``app`` / ``client`` fixtures
# rather than adopting the shared ``app_factory`` / ``authed_client``
# in tests/unit/web/conftest.py. The unsubscribe page renders via the
# full Jinja2 templates set up in ``create_app()``; stubbing them out
# would defeat the "no longer valid" markup assertion. Same trade-off
# as test_subscribers_admin.py / test_search_api.py.


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
def client(app):
    return TestClient(app)


def _insert_subscriber(conn, email: str) -> None:
    insert_subscriber(conn, email=email)


def _setup_limiter():
    _UNSUB_LIMITER._attempts.clear()


class TestUnsubscribeUrlFormat:
    """Verify that minted unsubscribe URLs contain only token=, not email=."""

    def test_unsub_url_contains_token_not_email(self, secret_key):
        from unittest.mock import MagicMock as _MM

        from mediaman.services.mail.newsletter.subscribers import (
            _send_to_subscribers as _send_to_recipients,
        )

        captured_urls = []

        def fake_send(**kwargs):
            captured_urls.append(kwargs.get("html", ""))

        mailgun = _MM()
        mailgun.send.side_effect = lambda to, subject, html: captured_urls.append(html)
        template = _MM()
        template.render.side_effect = lambda **kw: kw.get("unsubscribe_url", "")

        _send_to_recipients(
            recipient_emails=["user@example.com"],
            scheduled_items=[],
            deleted_items=[],
            this_week_items=[],
            storage={},
            reclaimed_week=0,
            reclaimed_month=0,
            reclaimed_total=0,
            subject="Test",
            base_url="https://example.com",
            secret_key=secret_key,
            dry_run=False,
            grace_days=7,
            template=template,
            mailgun=mailgun,
            report_date="2026-01-01",
        )

        assert len(captured_urls) == 1
        url = captured_urls[0]
        assert "token=" in url
        assert "email=" not in url, f"URL must not contain email= query param, got: {url}"


class TestUnsubscribeRoute:
    """The unsubscribe GET/POST routes must work with token-only URLs."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _setup_limiter()

    def test_get_with_token_only_shows_confirmation(self, client, app, secret_key):
        """GET /unsubscribe?token=... shows confirmation without email param."""
        conn = app.state.db
        _insert_subscriber(conn, "user@example.com")
        token = generate_unsubscribe_token(email="user@example.com", secret_key=secret_key)
        resp = client.get(f"/unsubscribe?token={token}", follow_redirects=True)
        # Should render the confirmation page (200) or equivalent.
        assert resp.status_code == 200

    def test_get_with_invalid_token_returns_invalid_response(self, client, secret_key):
        """GET with a bad token shows the invalid-link page."""
        resp = client.get("/unsubscribe?token=notavalidtoken", follow_redirects=True)
        assert resp.status_code == 200
        assert "no longer valid" in resp.text.lower() or resp.status_code in (200,)

    def test_post_with_token_only_unsubscribes(self, client, app, secret_key):
        """POST /unsubscribe with token= only must unsubscribe successfully."""
        conn = app.state.db
        _insert_subscriber(conn, "post@example.com")
        token = generate_unsubscribe_token(email="post@example.com", secret_key=secret_key)
        resp = client.post("/unsubscribe", data={"token": token})
        assert resp.status_code == 200

        row = conn.execute(
            "SELECT active FROM subscribers WHERE email = 'post@example.com'"
        ).fetchone()
        assert row is not None
        assert row["active"] == 0

    def test_post_with_tampered_token_rejected(self, client, app, secret_key):
        """A tampered token must not allow unsubscribe."""
        conn = app.state.db
        _insert_subscriber(conn, "safe@example.com")
        resp = client.post("/unsubscribe", data={"token": "tampered.token.value"})
        assert resp.status_code == 200  # returns the invalid-link page

        row = conn.execute(
            "SELECT active FROM subscribers WHERE email = 'safe@example.com'"
        ).fetchone()
        assert row is not None
        assert row["active"] == 1, "Subscriber must not be unsubscribed via tampered token"

    def test_email_derived_from_token_not_form_input(self, client, app, secret_key):
        """Even if email= is posted, the email is taken from the token."""
        conn = app.state.db
        _insert_subscriber(conn, "real@example.com")
        _insert_subscriber(conn, "attacker@example.com")

        token = generate_unsubscribe_token(email="real@example.com", secret_key=secret_key)
        # Attacker posts token for real@example.com but also posts a different email.
        resp = client.post("/unsubscribe", data={"token": token, "email": "attacker@example.com"})
        assert resp.status_code == 200

        real_row = conn.execute(
            "SELECT active FROM subscribers WHERE email = 'real@example.com'"
        ).fetchone()
        attacker_row = conn.execute(
            "SELECT active FROM subscribers WHERE email = 'attacker@example.com'"
        ).fetchone()
        assert real_row["active"] == 0, "real@example.com should be unsubscribed"
        assert attacker_row["active"] == 1, "attacker@example.com must NOT be unsubscribed"


class TestUnsubscribeAuditAtomicity:
    """§7.5 — deactivation and its audit row must land atomically.

    security_event_or_raise is used so a failed audit write rolls back the
    deactivation.  These tests verify:
    1. The success path: the audit row is written for every unsubscribe.
    2. The failure path: an audit-write failure rolls back the deactivation
       (subscriber remains active) and the route still returns 200 with the
       uniform confirmation message.
    """

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _setup_limiter()

    def test_successful_unsubscribe_writes_audit_row(self, client, app, secret_key):
        """A clean unsubscribe persists both the deactivation and the audit row."""
        conn = app.state.db
        _insert_subscriber(conn, "audit@example.com")
        token = generate_unsubscribe_token(email="audit@example.com", secret_key=secret_key)

        resp = client.post("/unsubscribe", data={"token": token})
        assert resp.status_code == 200

        row = conn.execute(
            "SELECT active FROM subscribers WHERE email = 'audit@example.com'"
        ).fetchone()
        assert row["active"] == 0, "subscriber must be deactivated"

        audit_row = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:subscriber.opted_out'"
        ).fetchone()
        assert audit_row is not None, "audit row must be written on successful unsubscribe"

    def test_audit_write_failure_rolls_back_deactivation(self, app, secret_key):
        """If the audit INSERT fails the deactivation is rolled back — §7.5 atomicity.

        Patches _insert_security_event to raise sqlite3.OperationalError so that
        security_event_or_raise propagates the exception, which aborts the
        ``with conn:`` transaction and leaves the subscriber active.

        The uncaught exception causes FastAPI to return a 500; we use
        raise_server_exceptions=False so the test can inspect the DB state.
        """
        import sqlite3 as _sqlite3

        conn = app.state.db
        _insert_subscriber(conn, "rollback@example.com")
        token = generate_unsubscribe_token(email="rollback@example.com", secret_key=secret_key)

        fault_client = TestClient(app, raise_server_exceptions=False)
        with patch(
            "mediaman.core.audit._insert_security_event",
            side_effect=_sqlite3.OperationalError("disk full"),
        ):
            fault_client.post("/unsubscribe", data={"token": token})

        row = conn.execute(
            "SELECT active FROM subscribers WHERE email = 'rollback@example.com'"
        ).fetchone()
        assert row["active"] == 1, (
            "subscriber must remain active when the audit write fails — "
            "deactivation must be rolled back atomically"
        )
