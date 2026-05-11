"""Tests for :mod:`mediaman.web.cookies`.

Covers the public surface of the cookies module:
  - ``SESSION_COOKIE_MAX_AGE`` — constant sanity
  - ``set_session_cookie`` — cookie attributes applied correctly

``is_request_secure`` and ``_secure_cookie_override`` are exercised by
:mod:`tests.unit.web.test_auth_routes` which still owns the broader
secure-cookie scheme behaviour suite (kept there to minimise churn when
the function moved out of ``mediaman.web.routes.auth``).
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from mediaman.web.cookies import SESSION_COOKIE_MAX_AGE, set_session_cookie

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
