"""Tests for HTML escaping in unsubscribe pages and newsletter recipient validation (C12)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.web.routes.subscribers import _validate_email
from mediaman.web.routes.subscribers import (
    router as subscribers_router,
)
from tests.helpers.factories import insert_subscriber

_TEMPLATE_DIR = (
    Path(__file__).parent.parent.parent.parent / "src" / "mediaman" / "web" / "templates"
)


def _templates_state() -> dict[str, object]:
    """Subscriber routes render via app.state.templates; the shared factory
    does not wire one up by default, so pass it as ``state_extras``."""
    return {"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))}


def _insert_subscriber(conn, email: str, active: int = 1) -> None:
    insert_subscriber(conn, email=email, active=active)


class TestUnsubscribeHtmlEscaping:
    """The Jinja2 templates auto-escape ``{{ email }}`` and ``{{ token }}``,
    so an attacker who lands a malicious payload in the unsubscribe URL
    cannot inject HTML/JS into the rendered confirmation page.

    The previous bespoke ``_unsub_confirm_html`` / ``_unsub_html``
    helpers duplicated the real templates — they have been removed and
    the routes render via Jinja2 directly. We exercise the live route
    so a regression in the template (e.g. someone replaces ``{{ email }}``
    with ``{{ email | safe }}``) is caught.
    """

    _KEY = "0123456789abcdef" * 4

    def _signed_token_for(self, email: str) -> str:
        from mediaman.crypto import generate_unsubscribe_token

        return generate_unsubscribe_token(email=email, secret_key=self._KEY)

    def test_confirm_page_escapes_email_from_token(self, app_factory, conn):
        """A malicious email payload baked into a valid token must be
        HTML-escaped by the Jinja2 template — no raw ``<script>`` ever
        reaches the rendered page."""
        app = app_factory(subscribers_router, conn=conn, state_extras=_templates_state())
        client = TestClient(app)

        malicious = '"><script>alert(1)</script>@evil.com'
        token = self._signed_token_for(malicious)
        resp = client.get(f"/unsubscribe?token={token}")

        # The route is configured to bail with a generic invalid-link
        # response if the token's email fails the inner sanity check;
        # we accept either outcome but neither must contain a raw
        # ``<script>`` injected from the email field.
        assert "<script>alert(1)" not in resp.text
        # Defensive: confirm the response renders normally rather than
        # 500ing.
        assert resp.status_code == 200

    def test_result_page_escapes_message(self, app_factory, conn):
        """The unsubscribe result page must HTML-escape the rendered
        message so an attacker cannot land an ``<img onerror>`` in the
        confirmation banner."""
        app = app_factory(subscribers_router, conn=conn, state_extras=_templates_state())

        # Render the result template directly to assert the auto-escape
        # behaviour without needing to manufacture a 429 path.
        from fastapi import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/unsubscribe",
            "headers": [],
            "app": app,
        }
        request = Request(scope)
        templates = app.state.templates
        rendered = templates.TemplateResponse(
            request,
            "subscribers/unsubscribe_result.html",
            {"message": "<img src=x onerror=alert(1)>@evil.com", "success": True},
        )
        body = bytes(rendered.body).decode()
        assert "<img" not in body
        assert "&lt;img" in body


class TestNewsletterRecipientHeaderInjection:
    """C12 — /api/newsletter/send must reject recipients containing CR/LF."""

    _KEY = "0123456789abcdef" * 4

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        from mediaman.web.routes.subscribers import _NEWSLETTER_LIMITER

        _NEWSLETTER_LIMITER.reset()

    def test_crlf_recipient_rejected_but_valid_still_sent(self, app_factory, authed_client, conn):
        """A subscriber row with embedded \\r\\n is skipped; clean rows still send."""
        # Clean row
        _insert_subscriber(conn, "good@example.com")
        # Poisoned row — simulates a DB compromise writing CRLF into the email.
        _insert_subscriber(conn, "evil@example.com\r\nBcc: attacker@evil.com")

        app = app_factory(subscribers_router, conn=conn, state_extras=_templates_state())
        client = authed_client(app, conn)

        captured: dict = {}

        def _fake_send(*, conn, secret_key, recipients, mark_notified):
            captured["recipients"] = list(recipients)

        with patch("mediaman.services.mail.newsletter.send_newsletter", side_effect=_fake_send):
            resp = client.post(
                "/api/newsletter/send",
                json={
                    "recipients": ["good@example.com", "evil@example.com\r\nBcc: attacker@evil.com"]
                },
            )

        # The clean recipient must still get the send — the poisoned row
        # is skipped but does not abort the flow.
        assert resp.status_code == 200
        assert captured["recipients"] == ["good@example.com"]
        for email in captured["recipients"]:
            assert "\r" not in email
            assert "\n" not in email

    def test_only_poisoned_recipient_returns_400(self, app_factory, authed_client, conn):
        """If every matching row is poisoned, caller gets 400 — no send."""
        _insert_subscriber(conn, "evil@example.com\r\nBcc: attacker@evil.com")
        app = app_factory(subscribers_router, conn=conn, state_extras=_templates_state())
        client = authed_client(app, conn)

        resp = client.post(
            "/api/newsletter/send",
            json={"recipients": ["evil@example.com\r\nBcc: attacker@evil.com"]},
        )
        assert resp.status_code == 400

    def test_validate_email_rejects_crlf(self):
        """The shared validator never accepts a CRLF-laden address."""
        assert not _validate_email("ok@ok.com\r\nBcc: x@y.com")
        assert not _validate_email("ok@ok.com\nX-Header: y")
