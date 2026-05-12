"""Additional tests for :mod:`mediaman.web.routes.auth` HTTP endpoints.

The existing test_auth_routes.py covers the pure helper functions
(_sanitise_log_field, is_request_secure). This file tests the actual
HTTP routes via TestClient:

  - GET  /login               — renders login page
  - POST /login               — happy path, bad creds, rate limit
  - POST /api/auth/logout     — happy path, unauthenticated
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.auth import _limiter
from mediaman.web.routes.auth import router as auth_router


@pytest.fixture
def _app(app_factory, conn, templates_stub):
    return app_factory(auth_router, conn=conn, state_extras={"templates": templates_stub})


def _create_admin(conn, username: str = "admin", password: str = "password1234") -> None:
    create_user(conn, username, password, enforce_policy=False)


class TestLoginPage:
    def test_get_login_returns_200(self, _app):
        """GET /login renders the login page."""
        client = TestClient(_app, raise_server_exceptions=True)
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_login_page_error_context_is_none(self, _app):
        """The login page template is invoked with error=None on a fresh GET."""
        client = TestClient(_app, raise_server_exceptions=True)
        resp = client.get("/login")
        ctx = resp.json()
        assert ctx.get("error") is None


class TestLoginSubmit:
    def setup_method(self):
        _limiter._attempts.clear()

    def test_valid_credentials_redirect_to_root(self, _app, conn):
        """POST /login with correct credentials redirects to /."""
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)

        with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
            resp = client.post(
                "/login",
                data={"username": "admin", "password": "password1234"},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_valid_login_sets_session_cookie(self, _app, conn):
        """A successful login sets a session_token cookie."""
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)

        with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
            resp = client.post(
                "/login",
                data={"username": "admin", "password": "password1234"},
                follow_redirects=False,
            )

        assert "session_token" in resp.cookies

    def test_invalid_credentials_render_error(self, _app, conn):
        """POST /login with wrong password renders the login page with an error."""
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)
        resp = client.post(
            "/login",
            data={"username": "admin", "password": "wrongpassword"},
        )
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx.get("error") is not None
        assert "Invalid" in ctx["error"]

    def test_no_session_cookie_on_failed_login(self, _app, conn):
        """A failed login must not set a session_token cookie."""
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)
        resp = client.post(
            "/login",
            data={"username": "admin", "password": "wrongpassword"},
        )
        assert "session_token" not in resp.cookies

    def test_rate_limit_blocks_after_5_attempts(self, _app, conn):
        """POST /login is rate-limited; after 5 failures from one IP the page shows throttle message."""
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)

        cap = _limiter._max_attempts
        for _ in range(cap):
            client.post("/login", data={"username": "admin", "password": "wrong"})

        resp = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx.get("error") is not None
        assert "Too many" in ctx["error"]

    def test_audit_log_written_on_failed_login(self, _app, conn):
        """A failed login attempt is recorded in the audit_log."""
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)
        client.post("/login", data={"username": "admin", "password": "wrong"})
        row = conn.execute(
            "SELECT action FROM audit_log WHERE action='sec:login.failed'"
        ).fetchone()
        assert row is not None

    def test_audit_log_written_on_successful_login(self, _app, conn):
        """A successful login is recorded in the audit_log."""
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)
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
    def test_logout_without_session_returns_401(self, _app):
        """POST /api/auth/logout without a session cookie returns 401."""
        client = TestClient(_app, raise_server_exceptions=True)
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 401

    def test_logout_with_invalid_token_returns_401(self, _app):
        """POST /api/auth/logout with a fake session token returns 401."""
        client = TestClient(_app, raise_server_exceptions=True)
        client.cookies.set("session_token", "not-a-real-token")
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 401

    def test_logout_happy_path_redirects_to_login(self, _app, conn):
        """POST /api/auth/logout with a valid session redirects to /login."""
        _create_admin(conn)
        token = create_session(conn, "admin")
        client = TestClient(_app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.post("/api/auth/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    def test_logout_invalidates_session(self, _app, conn):
        """After logout the session token is destroyed in the DB."""
        _create_admin(conn)
        token = create_session(conn, "admin")
        client = TestClient(_app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        client.post("/api/auth/logout")

        row = conn.execute(
            "SELECT token_hash FROM admin_sessions WHERE token_hash=?",
            (__import__("hashlib").sha256(token.encode()).hexdigest(),),
        ).fetchone()
        assert row is None

    def test_logout_clears_cookie_header(self, _app, conn):
        """The logout response includes a Set-Cookie header to clear session_token."""
        _create_admin(conn)
        token = create_session(conn, "admin")
        client = TestClient(_app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.post("/api/auth/logout", follow_redirects=False)
        set_cookie = resp.headers.get("set-cookie", "")
        assert "session_token" in set_cookie

    def test_logout_clears_cookie_with_explicit_path_and_samesite(self, _app, conn):
        """Deletion ``Set-Cookie`` must carry explicit ``Path=/`` and ``SameSite=strict``.

        RFC 6265bis matches a deletion against existing cookies by
        (name, domain, path).  If a future change altered the set path
        without updating the delete, the clear would silently fail.
        """
        _create_admin(conn)
        token = create_session(conn, "admin")
        client = TestClient(_app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.post("/api/auth/logout", follow_redirects=False)
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Path=/" in set_cookie
        # Starlette normalises samesite to lowercase in the Set-Cookie header.
        assert "samesite=strict" in set_cookie.lower()
        assert "httponly" in set_cookie.lower()

    def test_logout_with_invalid_token_clears_cookie(self, _app):
        """Stale-token logout must clear the cookie so the browser stops sending it."""
        client = TestClient(_app, raise_server_exceptions=True)
        client.cookies.set("session_token", "stale-fake-token")

        resp = client.post("/api/auth/logout")
        assert resp.status_code == 401
        set_cookie = resp.headers.get("set-cookie", "")
        # Either an explicit Max-Age=0 / expires-in-the-past or just
        # a Set-Cookie that names session_token with an expired timestamp
        # — either way the header must mention the cookie.
        assert "session_token" in set_cookie


class TestLoginRateLimitHeaders:
    """The rate-limit response must give clients a Retry-After hint."""

    def setup_method(self):
        _limiter._attempts.clear()

    def test_rate_limit_returns_retry_after(self, _app, conn):
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)

        for _ in range(_limiter._max_attempts):
            client.post("/login", data={"username": "admin", "password": "wrong"})

        resp = client.post("/login", data={"username": "admin", "password": "wrong"})
        # The response is rendered through the template stub so status is 200,
        # but the rate-limit branch must still set Retry-After.
        assert "retry-after" in {h.lower() for h in resp.headers}
        assert int(resp.headers["retry-after"]) > 0

    def test_rate_limit_window_is_300_seconds(self):
        """The login limiter window is the long, CGNAT-aware value."""
        # The exact value matters less than the order of magnitude — any
        # window <60s is too small for /24 IP buckets behind CGNAT.
        assert _limiter._window >= 60 * 5


class TestAuditUsernameSanitisation:
    """Failed-login audit entries must not stash an attacker-controlled
    multi-kilobyte / control-byte username verbatim into ``actor`` or
    the ``detail`` blob (which is rendered into the history page)."""

    def setup_method(self):
        _limiter._attempts.clear()

    def test_long_username_truncated_in_audit_row(self, _app, conn):
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)

        attacker_user = "A" * 5000
        client.post("/login", data={"username": attacker_user, "password": "wrong"})

        row = conn.execute(
            "SELECT actor, detail FROM audit_log WHERE action='sec:login.failed'"
        ).fetchone()
        assert row is not None
        # Neither column is allowed to carry the full 5000-char attacker
        # string.  ``actor`` and the ``actor=`` prefix in detail share
        # the same source.
        assert "A" * 5000 not in row["detail"]
        assert "A" * 5000 not in (row["actor"] or "")
        # Truncation marker proves the sanitiser ran on the actor.
        assert "..." in row["actor"]

    def test_control_chars_stripped_in_audit_row(self, _app, conn):
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)

        client.post(
            "/login",
            data={"username": "admin\x00\x1b[31m", "password": "wrong"},
        )

        row = conn.execute(
            "SELECT actor, detail FROM audit_log WHERE action='sec:login.failed'"
        ).fetchone()
        assert row is not None
        assert "\x00" not in row["detail"]
        assert "\x1b" not in row["detail"]
        assert "\x00" not in (row["actor"] or "")
        assert "\x1b" not in (row["actor"] or "")


class TestWeakPasswordCoalescing:
    """``password.weak_detected`` must be logged ONCE per flag-flip, not
    once per login of a flagged account."""

    def setup_method(self):
        _limiter._attempts.clear()

    def test_event_emitted_only_on_first_weak_login(self, _app, conn):
        _create_admin(conn, password="weak1234567a")  # passes minlength but weak overall
        client = TestClient(_app, raise_server_exceptions=True)

        with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
            # First login flips the flag.
            client.post("/login", data={"username": "admin", "password": "weak1234567a"})
            # Second login — flag is already set; no new audit row.
            client.post("/login", data={"username": "admin", "password": "weak1234567a"})
            # Third login — same.
            client.post("/login", data={"username": "admin", "password": "weak1234567a"})

        rows = conn.execute(
            "SELECT id FROM audit_log WHERE action='sec:password.weak_detected'"
        ).fetchall()
        assert len(rows) == 1


class TestUaHashInAuditLog:
    """``login.success`` audit detail must store a real hash, not the
    leading 80 chars of the raw user-agent."""

    def setup_method(self):
        _limiter._attempts.clear()

    def test_long_ua_stored_as_short_hash(self, _app, conn):
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)

        attacker_ua = "Mozilla/" + "X" * 5000
        with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
            client.post(
                "/login",
                data={"username": "admin", "password": "password1234"},
                headers={"User-Agent": attacker_ua},
            )

        row = conn.execute(
            "SELECT detail FROM audit_log WHERE action='sec:login.success'"
        ).fetchone()
        assert row is not None
        # No huge UA prefix should leak through.
        assert "X" * 80 not in row["detail"]
        # The hash field must look like 16 hex chars surrounded by quotes.
        import re

        match = re.search(r'"ua_hash":"([0-9a-f]+)"', row["detail"])
        assert match is not None
        assert len(match.group(1)) == 16
