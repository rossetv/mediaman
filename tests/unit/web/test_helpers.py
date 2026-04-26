"""Tests for :mod:`mediaman.web.routes._helpers`.

Covers the three exported symbols:
  - ``SESSION_COOKIE_MAX_AGE`` — constant sanity
  - ``set_session_cookie`` — cookie attributes applied correctly
  - ``is_admin`` — delegates to auth middleware with correct cookie name
  - ``fail`` — standardised JSON error envelope
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.responses import JSONResponse

from mediaman.web.routes._helpers import (
    SESSION_COOKIE_MAX_AGE,
    fail,
    is_admin,
    set_session_cookie,
)

# ---------------------------------------------------------------------------
# SESSION_COOKIE_MAX_AGE
# ---------------------------------------------------------------------------


def test_session_cookie_max_age_is_24_hours():
    """SESSION_COOKIE_MAX_AGE must be exactly 86400 seconds."""
    assert SESSION_COOKIE_MAX_AGE == 86400


# ---------------------------------------------------------------------------
# set_session_cookie
# ---------------------------------------------------------------------------


class TestSetSessionCookie:
    def _make_response(self) -> JSONResponse:
        return JSONResponse({"ok": True})

    def test_cookie_name_is_session_token(self):
        """The cookie must be named 'session_token'."""
        resp = self._make_response()
        set_session_cookie(resp, "abc123", secure=False)
        header = resp.headers.get("set-cookie", "")
        assert "session_token=abc123" in header

    def test_cookie_is_httponly(self):
        """The cookie must have the HttpOnly flag."""
        resp = self._make_response()
        set_session_cookie(resp, "tok", secure=False)
        header = resp.headers.get("set-cookie", "")
        assert "httponly" in header.lower()

    def test_cookie_samesite_strict(self):
        """The cookie must use SameSite=strict."""
        resp = self._make_response()
        set_session_cookie(resp, "tok", secure=False)
        header = resp.headers.get("set-cookie", "")
        assert "samesite=strict" in header.lower()

    def test_cookie_max_age_is_86400(self):
        """The cookie max_age must equal SESSION_COOKIE_MAX_AGE."""
        resp = self._make_response()
        set_session_cookie(resp, "tok", secure=False)
        header = resp.headers.get("set-cookie", "")
        assert "max-age=86400" in header.lower()

    def test_cookie_secure_flag_when_secure_true(self):
        """The Secure flag is set when secure=True."""
        resp = self._make_response()
        set_session_cookie(resp, "tok", secure=True)
        header = resp.headers.get("set-cookie", "")
        assert "secure" in header.lower()

    def test_cookie_no_secure_flag_when_secure_false(self):
        """The Secure flag is absent when secure=False."""
        resp = self._make_response()
        set_session_cookie(resp, "tok", secure=False)
        # starlette omits "secure" for insecure cookies
        parts = [p.strip().lower() for p in resp.headers.get("set-cookie", "").split(";")]
        # "secure" should not appear as a standalone flag
        assert "secure" not in parts

    def test_cookie_value_stored_correctly(self):
        """The token value must round-trip through the cookie header."""
        resp = self._make_response()
        token = "test-token-value-xyz"
        set_session_cookie(resp, token, secure=True)
        header = resp.headers.get("set-cookie", "")
        assert f"session_token={token}" in header


# ---------------------------------------------------------------------------
# is_admin
# ---------------------------------------------------------------------------


class TestIsAdmin:
    def _make_request(self, session_token: str | None) -> MagicMock:
        req = MagicMock()
        req.cookies = {"session_token": session_token} if session_token else {}
        return req

    def test_returns_true_when_middleware_returns_user(self):
        """is_admin returns True when get_optional_admin_from_token returns a username."""
        req = self._make_request("valid-token")
        with patch(
            "mediaman.auth.middleware.get_optional_admin_from_token",
            return_value="admin",
        ):
            assert is_admin(req) is True

    def test_returns_false_when_middleware_returns_none(self):
        """is_admin returns False when no valid session exists."""
        req = self._make_request(None)
        with patch(
            "mediaman.auth.middleware.get_optional_admin_from_token",
            return_value=None,
        ):
            assert is_admin(req) is False

    def test_passes_cookie_value_to_middleware(self):
        """is_admin must pass the session_token cookie value to the middleware function."""
        req = self._make_request("my-session-token")
        with patch(
            "mediaman.auth.middleware.get_optional_admin_from_token",
            return_value=None,
        ) as mock_fn:
            is_admin(req)
            mock_fn.assert_called_once()
            # First positional arg must be the token string
            assert mock_fn.call_args[0][0] == "my-session-token"

    def test_missing_cookie_passes_none_to_middleware(self):
        """When no cookie is present, None is passed to the middleware function."""
        req = self._make_request(None)
        with patch(
            "mediaman.auth.middleware.get_optional_admin_from_token",
            return_value=None,
        ) as mock_fn:
            is_admin(req)
            assert mock_fn.call_args[0][0] is None


# ---------------------------------------------------------------------------
# fail
# ---------------------------------------------------------------------------


class TestFail:
    def test_default_status_is_400(self):
        """fail() defaults to HTTP 400."""
        resp = fail("some_error", "Something went wrong")
        assert resp.status_code == 400

    def test_custom_status_code(self):
        """fail() honours a custom status code."""
        resp = fail("not_found", "Not found", status=404)
        assert resp.status_code == 404

    def test_response_contains_error_code(self):
        """The JSON body must include the machine-readable error code."""
        import json

        resp = fail("my_code", "human message")
        body = json.loads(resp.body)
        assert body["error"] == "my_code"

    def test_response_contains_message(self):
        """The JSON body must include the human-readable message."""
        import json

        resp = fail("code", "user-facing text")
        body = json.loads(resp.body)
        assert body["message"] == "user-facing text"

    def test_extra_kwargs_are_merged(self):
        """Extra keyword arguments are merged into the response body."""
        import json

        resp = fail("rate_limited", "Slow down", status=429, retry_after=60)
        body = json.loads(resp.body)
        assert body["retry_after"] == 60

    def test_response_is_json_response_instance(self):
        """fail() must return a JSONResponse."""
        resp = fail("err", "msg")
        assert isinstance(resp, JSONResponse)
