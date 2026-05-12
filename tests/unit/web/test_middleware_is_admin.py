"""Tests for :mod:`mediaman.web.auth.middleware`.

Covers all five callables exported from the module:

- :func:`is_admin` — convenience predicate over
  ``get_optional_admin_from_token``; used by page-rendering routes.
- :func:`get_current_admin` — strict FastAPI dependency; raises 401 when
  the session is absent or invalid.
- :func:`get_optional_admin` — soft dependency; returns the username or
  ``None``, never raises.
- :func:`get_optional_admin_from_token` — non-FastAPI variant; accepts a
  raw token string and an optional ``Request``.
- :func:`resolve_page_session` — page-route helper; returns
  ``(username, conn)`` or a ``302 /login`` redirect.

Tests that exercise real session validation hit the DB via the ``conn``
fixture so the fingerprint-binding and expiry logic are exercised for
real, not mocked away.  Tests that only care about the delegation
contract (e.g. ``is_admin`` → ``get_optional_admin_from_token``) use
``patch`` at the seam to keep them fast and focused.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

from mediaman.web.auth.middleware import (
    get_current_admin,
    get_optional_admin,
    get_optional_admin_from_token,
    is_admin,
    resolve_page_session,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_request(
    *,
    session_token: str | None = None,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> MagicMock:
    """Build a minimal mock :class:`starlette.requests.Request`.

    Only the attributes consulted by the auth-middleware callables are
    populated; everything else is left as a ``MagicMock`` default.
    """
    req = MagicMock()
    req.cookies = {"session_token": session_token} if session_token else {}
    req.headers = {}
    if user_agent is not None:
        req.headers["user-agent"] = user_agent
    # ``req.headers.get(...)`` must delegate to the dict.
    req.headers.get = lambda key, default=None: (
        req.headers.get(key, default) if isinstance(req.headers, dict) else default
    )
    return req


def _real_request(
    *,
    session_token: str | None = None,
    user_agent: str = "Mozilla/5.0 Test",
    client_ip: str = "testclient",
) -> MagicMock:
    """Build a mock :class:`starlette.requests.Request` with real attribute paths.

    The auth helpers call ``request.headers.get("user-agent")`` and
    ``request.client.host`` (via ``get_client_ip``).
    ``MagicMock().headers.get(...)`` would return another ``MagicMock``
    (truthy, not a string), breaking the fingerprint check.  This
    wrapper ensures the header lookup returns a proper string and the
    client IP is also a string so the fingerprint computation is
    deterministic.
    """
    req = MagicMock()
    req.cookies = {"session_token": session_token} if session_token else {}
    req.headers.get = lambda key, default=None: (
        user_agent if key == "user-agent" else default
    )
    req.client.host = client_ip
    return req


# ---------------------------------------------------------------------------
# is_admin
# ---------------------------------------------------------------------------


class TestIsAdmin:
    """Convenience predicate wrapper over ``get_optional_admin_from_token``."""

    def test_returns_true_when_middleware_returns_user(self):
        """is_admin returns True when get_optional_admin_from_token returns a username."""
        req = _real_request(session_token="valid-token")
        with patch(
            "mediaman.web.auth.middleware.get_optional_admin_from_token",
            return_value="admin",
        ):
            assert is_admin(req) is True

    def test_returns_false_when_middleware_returns_none(self):
        """is_admin returns False when no valid session exists."""
        req = _real_request(session_token=None)
        with patch(
            "mediaman.web.auth.middleware.get_optional_admin_from_token",
            return_value=None,
        ):
            assert is_admin(req) is False

    def test_passes_cookie_value_to_middleware(self):
        """is_admin must pass the session_token cookie value to the middleware function."""
        req = _real_request(session_token="my-session-token")
        with patch(
            "mediaman.web.auth.middleware.get_optional_admin_from_token",
            return_value=None,
        ) as mock_fn:
            is_admin(req)
            mock_fn.assert_called_once()
            # First positional arg must be the token string
            assert mock_fn.call_args[0][0] == "my-session-token"

    def test_missing_cookie_passes_none_to_middleware(self):
        """When no cookie is present, None is passed to the middleware function."""
        req = _real_request(session_token=None)
        with patch(
            "mediaman.web.auth.middleware.get_optional_admin_from_token",
            return_value=None,
        ) as mock_fn:
            is_admin(req)
            assert mock_fn.call_args[0][0] is None


# ---------------------------------------------------------------------------
# get_current_admin (requires real DB for meaningful tests)
# ---------------------------------------------------------------------------


class TestGetCurrentAdmin:
    """Strict FastAPI dependency — returns username or raises HTTP 401."""

    def _make_authed_app(self, conn) -> tuple[FastAPI, TestClient]:
        """Build a minimal app whose single route uses ``get_current_admin``."""
        from mediaman.db import set_connection
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        app = FastAPI()
        set_connection(conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        @app.get("/me")
        def _me(username: str = pytest.approx(get_current_admin)):  # type: ignore[assignment]
            return {"username": username}

        # Wire the dependency manually via Depends so TestClient resolves it.
        from fastapi import Depends

        @app.get("/who")
        def _who(username: str = Depends(get_current_admin)):
            return {"username": username}

        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("session_token", token)
        return app, client, token

    def test_valid_session_returns_username(self, conn):
        """A request with a valid session_token cookie must return the username."""
        from fastapi import Depends

        from mediaman.db import set_connection
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        app = FastAPI()
        set_connection(conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        @app.get("/who")
        def _who(username: str = Depends(get_current_admin)):
            return {"username": username}

        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("session_token", token)
        resp = client.get("/who")
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"

    def test_missing_token_raises_401(self, conn):
        """No session_token cookie → 401 Unauthenticated."""
        from fastapi import Depends

        from mediaman.db import set_connection

        app = FastAPI()
        set_connection(conn)

        @app.get("/who")
        def _who(username: str = Depends(get_current_admin)):
            return {"username": username}

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/who")
        assert resp.status_code == 401

    def test_invalid_token_raises_401(self, conn):
        """A token that does not exist in the DB → 401."""
        from fastapi import Depends

        from mediaman.db import set_connection

        app = FastAPI()
        set_connection(conn)

        @app.get("/who")
        def _who(username: str = Depends(get_current_admin)):
            return {"username": username}

        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("session_token", "a" * 64)
        resp = client.get("/who")
        assert resp.status_code == 401

    def test_error_detail_does_not_leak_session_state(self, conn):
        """The 401 detail message must be the uniform ``Not authenticated``
        string — not ``Session expired`` or any other state-leaking variant."""
        from fastapi import Depends

        from mediaman.db import set_connection

        app = FastAPI()
        set_connection(conn)

        @app.get("/who")
        def _who(username: str = Depends(get_current_admin)):
            return {"username": username}

        client = TestClient(app, raise_server_exceptions=False)
        # Missing cookie path.
        resp = client.get("/who")
        assert resp.status_code == 401
        body = resp.json()
        assert body.get("detail") == "Not authenticated"


# ---------------------------------------------------------------------------
# get_optional_admin (soft dependency — never raises)
# ---------------------------------------------------------------------------


class TestGetOptionalAdmin:
    """Soft dependency — returns username or None; never raises."""

    def test_valid_session_returns_username(self, conn):
        """A valid session cookie → username string."""
        from fastapi import Depends

        from mediaman.db import set_connection
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        app = FastAPI()
        set_connection(conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        @app.get("/who")
        def _who(username: str | None = Depends(get_optional_admin)):
            return {"username": username}

        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)
        resp = client.get("/who")
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"

    def test_missing_token_returns_none(self, conn):
        """No session cookie → endpoint sees ``None``; no 401 raised."""
        from fastapi import Depends

        from mediaman.db import set_connection

        app = FastAPI()
        set_connection(conn)

        @app.get("/who")
        def _who(username: str | None = Depends(get_optional_admin)):
            return {"username": username}

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/who")
        assert resp.status_code == 200
        assert resp.json()["username"] is None

    def test_invalid_token_returns_none(self, conn):
        """A token not present in the DB → ``None``, not an exception."""
        from fastapi import Depends

        from mediaman.db import set_connection

        app = FastAPI()
        set_connection(conn)

        @app.get("/who")
        def _who(username: str | None = Depends(get_optional_admin)):
            return {"username": username}

        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", "b" * 64)
        resp = client.get("/who")
        assert resp.status_code == 200
        assert resp.json()["username"] is None


# ---------------------------------------------------------------------------
# get_optional_admin_from_token
# ---------------------------------------------------------------------------


class TestGetOptionalAdminFromToken:
    """Non-FastAPI entrypoint for nullable session validation.

    Fingerprint binding is best-effort: if no ``request`` is supplied the
    UA/IP check is skipped entirely.
    """

    def test_valid_token_without_request_returns_username(self, conn):
        """Valid token, no request → username returned (no fingerprint check)."""
        from mediaman.db import set_connection
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        set_connection(conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        result = get_optional_admin_from_token(token)
        assert result == "admin"

    def test_none_token_returns_none(self, conn):
        """``None`` token → ``None`` without touching the DB."""
        from mediaman.db import set_connection

        set_connection(conn)
        assert get_optional_admin_from_token(None) is None

    def test_empty_string_token_returns_none(self, conn):
        """An empty string token must return ``None`` (falsy guard)."""
        from mediaman.db import set_connection

        set_connection(conn)
        assert get_optional_admin_from_token("") is None

    def test_invalid_token_returns_none(self, conn):
        """A syntactically valid but unknown token → ``None``."""
        from mediaman.db import set_connection

        set_connection(conn)
        assert get_optional_admin_from_token("c" * 64) is None

    def test_valid_token_with_request_passes_fingerprint(self, conn):
        """When a ``request`` is supplied, UA and client IP are forwarded to
        ``validate_session``; a session issued with the same UA/IP passes.

        ``client_ip`` is set to ``"testclient"`` to match the string that
        ``get_client_ip`` extracts from the mock's ``request.client.host``.
        """
        from mediaman.db import set_connection
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        set_connection(conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(
            conn, "admin", user_agent="Mozilla/5.0 Test", client_ip="testclient"
        )

        req = _real_request(session_token=token, user_agent="Mozilla/5.0 Test", client_ip="testclient")
        result = get_optional_admin_from_token(token, request=req)
        assert result == "admin"

    def test_mismatched_ua_returns_none(self, conn):
        """A session issued with one UA and replayed with a different UA is
        rejected by the fingerprint check → ``None`` returned."""
        from mediaman.db import set_connection
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        set_connection(conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(
            conn, "admin", user_agent="Mozilla/5.0 Firefox", client_ip="testclient"
        )

        req = _real_request(session_token=token, user_agent="curl/8.0 attacker", client_ip="testclient")
        result = get_optional_admin_from_token(token, request=req)
        assert result is None


# ---------------------------------------------------------------------------
# resolve_page_session
# ---------------------------------------------------------------------------


class TestResolvePageSession:
    """Page-route helper — returns ``(username, conn)`` or a 302 redirect."""

    def test_valid_session_returns_username_and_conn(self, conn):
        """Valid cookie → ``(username, conn)`` tuple."""
        from mediaman.db import set_connection
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        set_connection(conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        req = _real_request(session_token=token, user_agent="Mozilla/5.0 Test")
        result = resolve_page_session(req)
        assert isinstance(result, tuple)
        username, db_conn = result
        assert username == "admin"
        assert db_conn is not None

    def test_missing_cookie_returns_redirect(self, conn):
        """No session cookie → ``RedirectResponse("/login", 302)``."""
        from mediaman.db import set_connection

        set_connection(conn)
        req = _real_request(session_token=None)
        result = resolve_page_session(req)
        assert isinstance(result, RedirectResponse)
        assert result.status_code == 302
        assert result.headers["location"] == "/login"

    def test_invalid_token_returns_redirect(self, conn):
        """A token that does not exist in the DB → ``RedirectResponse``."""
        from mediaman.db import set_connection

        set_connection(conn)
        req = _real_request(session_token="d" * 64)
        result = resolve_page_session(req)
        assert isinstance(result, RedirectResponse)
        assert result.status_code == 302

    def test_mismatched_ua_returns_redirect(self, conn):
        """Stolen cookie replayed from a different User-Agent → redirect."""
        from mediaman.db import set_connection
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        set_connection(conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(
            conn, "admin", user_agent="Mozilla/5.0 Firefox", client_ip="testclient"
        )

        req = _real_request(session_token=token, user_agent="curl/8.0 attacker", client_ip="testclient")
        result = resolve_page_session(req)
        assert isinstance(result, RedirectResponse)
        assert result.status_code == 302
        assert result.headers["location"] == "/login"

    def test_redirect_location_is_login(self, conn):
        """The redirect target must specifically be ``/login`` (not ``/`` or
        any other URL) so existing page-route contracts are not broken."""
        from mediaman.db import set_connection

        set_connection(conn)
        req = _real_request(session_token=None)
        result = resolve_page_session(req)
        assert isinstance(result, RedirectResponse)
        assert result.headers["location"] == "/login"
