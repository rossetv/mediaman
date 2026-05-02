"""Tests for the security-event surface in the history API (M27).

Covers:
- ``GET /api/history?action=security`` returns every ``sec:*`` row.
- ``GET /api/security-events`` is the dedicated endpoint and returns
  the same shape.
- A row's ``is_security`` flag is True for ``sec:*`` actions and the
  badge / label do not get clobbered by the media-action defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.audit import security_event
from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.history import router as history_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(history_router)
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


def _add_media_audit(conn, action: str = "scanned") -> None:
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, created_at) VALUES (?, ?, ?)",
        ("m1", action, datetime.now(UTC).isoformat()),
    )
    conn.commit()


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


class TestSecurityFilter:
    def test_security_filter_returns_only_sec_events(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        # Mix of media + security events.
        _add_media_audit(conn, action="scanned")
        _add_media_audit(conn, action="deleted")
        security_event(conn, event="login.success", actor="admin", ip="127.0.0.1")
        security_event(conn, event="settings.write", actor="admin", ip="127.0.0.1")

        resp = client.get("/api/history?action=security")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        for item in body["items"]:
            assert item["action"].startswith("sec:")
            assert item["is_security"] is True

    def test_security_endpoint_dedicated(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        security_event(conn, event="reauth.granted", actor="admin")
        _add_media_audit(conn, action="scanned")  # noise

        resp = client.get("/api/security-events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["action"] == "sec:reauth.granted"
        assert item["title"] == "reauth.granted"
        assert item["is_security"] is True
        assert item["badge_class"] == "badge-action-security"

    def test_security_events_requires_auth(self, conn, secret_key):
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/security-events")
        assert resp.status_code == 401

    def test_default_history_includes_security_rows(self, conn, secret_key):
        """The unfiltered /api/history must surface security rows too —
        an operator paging through history shouldn't have to know about
        the synthetic filter to see what happened."""
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        security_event(conn, event="login.success", actor="admin")
        _add_media_audit(conn, action="scanned")

        resp = client.get("/api/history")
        assert resp.status_code == 200
        actions = [item["action"] for item in resp.json()["items"]]
        assert "sec:login.success" in actions
        assert "scanned" in actions
