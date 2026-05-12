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

from fastapi.testclient import TestClient

from mediaman.core.audit import security_event
from mediaman.web.routes.history import router as history_router


def _add_media_audit(conn, action: str = "scanned") -> None:
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, created_at) VALUES (?, ?, ?)",
        ("m1", action, datetime.now(UTC).isoformat()),
    )
    conn.commit()


class TestSecurityFilter:
    def test_security_filter_returns_only_sec_events(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

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

    def test_security_endpoint_dedicated(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

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

    def test_security_events_requires_auth(self, app_factory, conn):
        app = app_factory(history_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/security-events")
        assert resp.status_code == 401

    def test_default_history_includes_security_rows(self, app_factory, authed_client, conn):
        """The unfiltered /api/history must surface security rows too —
        an operator paging through history shouldn't have to know about
        the synthetic filter to see what happened."""
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

        security_event(conn, event="login.success", actor="admin")
        _add_media_audit(conn, action="scanned")

        resp = client.get("/api/history")
        assert resp.status_code == 200
        actions = [item["action"] for item in resp.json()["items"]]
        assert "sec:login.success" in actions
        assert "scanned" in actions
