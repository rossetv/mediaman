"""Tests for POST /api/newsletter/send re-auth enforcement.

The endpoint fires a full newsletter blast to the Mailgun-configured
subscriber list, so a compromised session cookie must not be enough —
the caller must re-confirm their password. These tests pin that contract
so a future refactor cannot silently drop the check.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.subscribers import router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


class TestNewsletterSendReauth:
    def test_missing_password_rejected_with_403(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.post(
            "/api/newsletter/send",
            json={"recipients": ["someone@example.com"]},
        )

        assert resp.status_code == 403
        body = resp.json()
        assert body["ok"] is False
        assert "password" in body["error"].lower()

    def test_wrong_password_rejected_with_403(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.post(
            "/api/newsletter/send",
            json={
                "recipients": ["someone@example.com"],
                "confirm_password": "not-the-password",
            },
        )

        assert resp.status_code == 403

    def test_correct_password_reaches_send(self, conn, secret_key):
        """With the right password the handler proceeds past the re-auth
        gate. We don't care about the downstream send succeeding — only
        that the 403 is not returned and the send function is called."""
        from datetime import datetime, timezone

        # Seed an active subscriber so the allowed-list query finds them.
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO subscribers (email, active, created_at) VALUES (?, 1, ?)",
            ("target@example.com", now),
        )
        conn.commit()

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        with patch("mediaman.services.newsletter.send_newsletter") as mock_send:
            resp = client.post(
                "/api/newsletter/send",
                json={
                    "recipients": ["target@example.com"],
                    "confirm_password": "password1234",
                },
            )

        assert resp.status_code == 200
        assert mock_send.called

    def test_header_accepted_as_password_source(self, conn, secret_key):
        """X-Confirm-Password header is honoured as an alternative to the
        body field — matches the pattern used by api_delete_user."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO subscribers (email, active, created_at) VALUES (?, 1, ?)",
            ("target@example.com", now),
        )
        conn.commit()

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        with patch("mediaman.services.newsletter.send_newsletter"):
            resp = client.post(
                "/api/newsletter/send",
                json={"recipients": ["target@example.com"]},
                headers={"X-Confirm-Password": "password1234"},
            )

        assert resp.status_code == 200
