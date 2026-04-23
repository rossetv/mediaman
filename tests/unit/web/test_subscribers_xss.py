"""Tests for HTML escaping in unsubscribe pages and newsletter recipient validation (C12)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.subscribers import (
    _unsub_confirm_html,
    _unsub_html,
    _validate_email,
)
from mediaman.web.routes.subscribers import (
    router as subscribers_router,
)


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(subscribers_router)
    app.state.config = Config(secret_key=secret_key)
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


def _insert_subscriber(conn, email: str, active: int = 1) -> None:
    conn.execute(
        "INSERT INTO subscribers (email, active, created_at) VALUES (?, ?, ?)",
        (email, active, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


class TestUnsubscribeHtmlEscaping:
    def test_confirm_html_escapes_email(self):
        malicious = '"><script>alert(1)</script>@evil.com'
        html = _unsub_confirm_html(malicious, "safe-token")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_confirm_html_escapes_token(self):
        malicious_token = '"><script>alert(2)</script>'
        html = _unsub_confirm_html("safe@example.com", malicious_token)
        assert "<script>alert(2)" not in html

    def test_result_html_escapes_message(self):
        malicious = '<img src=x onerror=alert(1)>@evil.com is already unsubscribed.'
        html = _unsub_html(malicious, success=True)
        # The < and > must be escaped so the img tag never opens in the browser.
        assert "<img" not in html
        assert "&lt;img" in html


class TestNewsletterRecipientHeaderInjection:
    """C12 — /api/newsletter/send must reject recipients containing CR/LF."""

    _KEY = "0123456789abcdef" * 4

    def setup_method(self):
        from mediaman.web.routes.subscribers import _NEWSLETTER_LIMITER
        _NEWSLETTER_LIMITER._attempts.clear()
        _NEWSLETTER_LIMITER._day_counts.clear()

    def test_crlf_recipient_rejected_but_valid_still_sent(self, db_path):
        """A subscriber row with embedded \\r\\n is skipped; clean rows still send."""
        conn = init_db(str(db_path))
        # Clean row
        _insert_subscriber(conn, "good@example.com")
        # Poisoned row — simulates a DB compromise writing CRLF into the email.
        _insert_subscriber(conn, "evil@example.com\r\nBcc: attacker@evil.com")

        app = _make_app(conn, self._KEY)
        client = _auth_client(app, conn)

        captured: dict = {}

        def _fake_send(*, conn, secret_key, recipients, mark_notified):
            captured["recipients"] = list(recipients)

        with patch("mediaman.services.newsletter.send_newsletter", side_effect=_fake_send):
            resp = client.post(
                "/api/newsletter/send",
                json={"recipients": ["good@example.com", "evil@example.com\r\nBcc: attacker@evil.com"]},
            )

        # The clean recipient must still get the send — the poisoned row
        # is skipped but does not abort the flow.
        assert resp.status_code == 200
        assert captured["recipients"] == ["good@example.com"]
        for email in captured["recipients"]:
            assert "\r" not in email
            assert "\n" not in email

    def test_only_poisoned_recipient_returns_400(self, db_path):
        """If every matching row is poisoned, caller gets 400 — no send."""
        conn = init_db(str(db_path))
        _insert_subscriber(conn, "evil@example.com\r\nBcc: attacker@evil.com")
        app = _make_app(conn, self._KEY)
        client = _auth_client(app, conn)

        resp = client.post(
            "/api/newsletter/send",
            json={"recipients": ["evil@example.com\r\nBcc: attacker@evil.com"]},
        )
        assert resp.status_code == 400

    def test_validate_email_rejects_crlf(self):
        """The shared validator never accepts a CRLF-laden address."""
        assert not _validate_email("ok@ok.com\r\nBcc: x@y.com")
        assert not _validate_email("ok@ok.com\nX-Header: y")
