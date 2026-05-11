"""Tests for :func:`mediaman.web.auth.middleware.is_admin`.

The predicate is the small convenience wrapper over
``get_optional_admin_from_token`` that page-rendering routes use to gate
admin-only UI affordances (keep, kept, recommended).  These tests pin its
delegation contract so a refactor of the underlying validator does not
silently change the predicate's behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mediaman.web.auth.middleware import is_admin


class TestIsAdmin:
    def _make_request(self, session_token: str | None) -> MagicMock:
        req = MagicMock()
        req.cookies = {"session_token": session_token} if session_token else {}
        return req

    def test_returns_true_when_middleware_returns_user(self):
        """is_admin returns True when get_optional_admin_from_token returns a username."""
        req = self._make_request("valid-token")
        with patch(
            "mediaman.web.auth.middleware.get_optional_admin_from_token",
            return_value="admin",
        ):
            assert is_admin(req) is True

    def test_returns_false_when_middleware_returns_none(self):
        """is_admin returns False when no valid session exists."""
        req = self._make_request(None)
        with patch(
            "mediaman.web.auth.middleware.get_optional_admin_from_token",
            return_value=None,
        ):
            assert is_admin(req) is False

    def test_passes_cookie_value_to_middleware(self):
        """is_admin must pass the session_token cookie value to the middleware function."""
        req = self._make_request("my-session-token")
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
        req = self._make_request(None)
        with patch(
            "mediaman.web.auth.middleware.get_optional_admin_from_token",
            return_value=None,
        ) as mock_fn:
            is_admin(req)
            assert mock_fn.call_args[0][0] is None
