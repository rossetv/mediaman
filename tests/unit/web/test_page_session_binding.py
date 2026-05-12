"""Regression tests for session fingerprint binding on page routes.

Previously each page route called ``validate_session(conn, token)`` with
no UA/IP, which silently bypassed the fingerprint check. These tests
exercise the new :func:`mediaman.auth.middleware.resolve_page_session`
helper via a real page route (``/library``) to confirm a stolen cookie
replayed from a different User-Agent is redirected to ``/login`` rather
than granted access.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.library import router as library_router


def _issue_session(conn, *, ua: str, ip: str) -> str:
    create_user(conn, "alice", "test-password-long-enough", enforce_policy=False)
    return create_session(conn, "alice", user_agent=ua, client_ip=ip)


class TestPageSessionBinding:
    def test_library_accepts_matching_fingerprint(self, app_factory, conn, templates_stub):
        # Use the client_ip TestClient actually reports ("testclient") so the
        # IP-prefix component of the fingerprint matches.
        token = _issue_session(conn, ua="Mozilla/5.0 Firefox", ip="testclient")

        app = app_factory(library_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.get(
            "/library",
            headers={"User-Agent": "Mozilla/5.0 Firefox"},
            follow_redirects=False,
        )
        # 200 from the stubbed template response.
        assert resp.status_code == 200

    def test_library_redirects_on_different_ua(self, app_factory, conn, templates_stub):
        """Stolen cookie replayed with a different UA → redirect to /login."""
        token = _issue_session(conn, ua="Mozilla/5.0 Firefox", ip="testclient")

        app = app_factory(library_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.get(
            "/library",
            headers={"User-Agent": "curl/8.0 attacker"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_library_redirects_when_no_cookie(self, app_factory, conn, templates_stub):
        app = app_factory(library_router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/library", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"
