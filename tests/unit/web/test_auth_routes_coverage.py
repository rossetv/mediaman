"""Additional tests for :mod:`mediaman.web.routes.auth` HTTP endpoints.

The existing test_auth_routes.py covers the pure helper functions
(_sanitise_log_field, is_request_secure). This file tests the actual
HTTP routes via TestClient:

  - GET  /login               — renders login page
  - POST /login               — happy path, bad creds, rate limit
  - POST /api/auth/logout     — happy path, unauthenticated
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.auth import _limiter
from mediaman.web.routes.auth import router as auth_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)

    mock_templates = MagicMock()

    def fake_template_response(request, template_name, ctx):
        return HTMLResponse(json.dumps(ctx, default=str), status_code=200)

    mock_templates.TemplateResponse.side_effect = fake_template_response
    app.state.templates = mock_templates
    return app


def _create_admin(conn, username: str = "admin", password: str = "password1234") -> None:
    create_user(conn, username, password, enforce_policy=False)


class TestLoginPage:
    def test_get_login_returns_200(self, db_path, secret_key):
        """GET /login renders the login page."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_login_page_error_context_is_none(self, db_path, secret_key):
        """The login page template is invoked with error=None on a fresh GET."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/login")
        ctx = resp.json()
        assert ctx.get("error") is None


class TestLoginSubmit:
    def setup_method(self):
        _limiter._attempts.clear()

    def test_valid_credentials_redirect_to_root(self, db_path, secret_key):
        """POST /login with correct credentials redirects to /."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
            resp = client.post(
                "/login",
                data={"username": "admin", "password": "password1234"},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_valid_login_sets_session_cookie(self, db_path, secret_key):
        """A successful login sets a session_token cookie."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
            resp = client.post(
                "/login",
                data={"username": "admin", "password": "password1234"},
                follow_redirects=False,
            )

        assert "session_token" in resp.cookies

    def test_invalid_credentials_render_error(self, db_path, secret_key):
        """POST /login with wrong password renders the login page with an error."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/login",
            data={"username": "admin", "password": "wrongpassword"},
        )
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx.get("error") is not None
        assert "Invalid" in ctx["error"]

    def test_no_session_cookie_on_failed_login(self, db_path, secret_key):
        """A failed login must not set a session_token cookie."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/login",
            data={"username": "admin", "password": "wrongpassword"},
        )
        assert "session_token" not in resp.cookies

    def test_rate_limit_blocks_after_5_attempts(self, db_path, secret_key):
        """POST /login is rate-limited; after 5 failures from one IP the page shows throttle message."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        cap = _limiter._max_attempts
        for _ in range(cap):
            client.post("/login", data={"username": "admin", "password": "wrong"})

        resp = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx.get("error") is not None
        assert "Too many" in ctx["error"]

    def test_audit_log_written_on_failed_login(self, db_path, secret_key):
        """A failed login attempt is recorded in the audit_log."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        client.post("/login", data={"username": "admin", "password": "wrong"})
        row = conn.execute(
            "SELECT action FROM audit_log WHERE action='sec:login.failed'"
        ).fetchone()
        assert row is not None

    def test_audit_log_written_on_successful_login(self, db_path, secret_key):
        """A successful login is recorded in the audit_log."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
            client.post(
                "/login",
                data={"username": "admin", "password": "password1234"},
            )
        row = conn.execute(
            "SELECT action FROM audit_log WHERE action='sec:login.success'"
        ).fetchone()
        assert row is not None


class TestLogout:
    def test_logout_without_session_returns_401(self, db_path, secret_key):
        """POST /api/auth/logout without a session cookie returns 401."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 401

    def test_logout_with_invalid_token_returns_401(self, db_path, secret_key):
        """POST /api/auth/logout with a fake session token returns 401."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", "not-a-real-token")
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 401

    def test_logout_happy_path_redirects_to_login(self, db_path, secret_key):
        """POST /api/auth/logout with a valid session redirects to /login."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        token = create_session(conn, "admin")
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.post("/api/auth/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    def test_logout_invalidates_session(self, db_path, secret_key):
        """After logout the session token is destroyed in the DB."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        token = create_session(conn, "admin")
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        client.post("/api/auth/logout")

        row = conn.execute(
            "SELECT token_hash FROM admin_sessions WHERE token_hash=?",
            (__import__("hashlib").sha256(token.encode()).hexdigest(),),
        ).fetchone()
        assert row is None

    def test_logout_clears_cookie_header(self, db_path, secret_key):
        """The logout response includes a Set-Cookie header to clear session_token."""
        conn = init_db(str(db_path))
        _create_admin(conn)
        token = create_session(conn, "admin")
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.post("/api/auth/logout", follow_redirects=False)
        set_cookie = resp.headers.get("set-cookie", "")
        assert "session_token" in set_cookie
