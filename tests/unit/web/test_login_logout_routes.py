"""`POST /login` and `POST /api/auth/logout` over HTTP: sessions, cookies and audit rows.

The page itself, the rate limit and the return address have their own files
(`test_login_page.py`, `test_login_limit.py`, `test_login_return.py`).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session, validate_session
from mediaman.web.routes.auth import _limiter
from mediaman.web.routes.auth import router as auth_router


@pytest.fixture
def _app(app_factory, conn, templates_stub):
    return app_factory(auth_router, conn=conn, state_extras={"templates": templates_stub})


def _create_admin(conn, username: str = "admin", password: str = "password1234") -> None:
    create_user(conn, username, password, enforce_policy=False)


class TestLoginSubmit:
    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _limiter.reset()

    def test_valid_login_redirects_to_root_with_a_session_cookie(self, _app, conn):
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
        assert "session_token" in resp.cookies

    def test_no_session_cookie_on_failed_login(self, _app, conn):
        _create_admin(conn)
        client = TestClient(_app, raise_server_exceptions=True)
        resp = client.post(
            "/login",
            data={"username": "admin", "password": "wrongpassword"},
        )
        assert "session_token" not in resp.cookies

    def test_a_failed_login_leaves_the_session_already_there(self, _app, conn):
        _create_admin(conn)
        client = TestClient(_app)
        token = create_session(conn, "admin")
        client.cookies.set("session_token", token)

        client.post("/login", data={"username": "admin", "password": "WRONG"})

        assert validate_session(conn, token) == "admin"

    def test_a_login_leaves_the_persons_sessions_on_other_devices(self, _app, conn):
        _create_admin(conn)
        elsewhere = create_session(conn, "admin")
        client = TestClient(_app, follow_redirects=False)
        client.cookies.set("session_token", create_session(conn, "admin"))

        resp = client.post("/login", data={"username": "admin", "password": "password1234"})

        assert resp.status_code == 302
        assert validate_session(conn, elsewhere) == "admin"

    def test_wrong_passwords_from_one_client_do_not_lock_out_another(self, _app, conn):
        _create_admin(conn)
        guesser = TestClient(_app, client=("198.51.100.4", 50000))
        for _ in range(5):
            guesser.post("/login", data={"username": "admin", "password": "wrong"})

        owner = TestClient(_app, client=("203.0.113.7", 50000))
        with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
            resp = owner.post(
                "/login",
                data={"username": "admin", "password": "password1234"},
                follow_redirects=False,
            )

        assert resp.status_code == 302


class TestLogout:
    def test_logout_without_session_returns_401(self, _app):
        client = TestClient(_app, raise_server_exceptions=True)
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


class TestAuditUsernameSanitisation:
    """Failed-login audit entries must not stash an attacker-controlled
    multi-kilobyte / control-byte username verbatim into ``actor`` or
    the ``detail`` blob (which is rendered into the history page)."""

    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _limiter.reset()

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

    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _limiter.reset()

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

    @pytest.fixture(autouse=True)
    def _clear_limiter(self):
        _limiter.reset()

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
