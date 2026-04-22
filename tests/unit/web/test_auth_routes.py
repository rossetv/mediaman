"""Tests for :mod:`mediaman.web.routes.auth_routes` helpers.

``is_request_secure`` now defaults to True on a public deployment
(the common case); operators who genuinely need plaintext cookies in
local dev can set ``MEDIAMAN_FORCE_SECURE_COOKIES=false``. This is
the opposite of the previous default — accepting the previous
"fail-open to plaintext" behaviour was the root cause of the cookie
downgrade bug found in the security audit.
"""

from unittest.mock import MagicMock

from mediaman.web.routes.auth import is_request_secure


def _request(headers=None, peer="203.0.113.99", scheme="http"):
    """Build a minimal FastAPI Request-alike for ``is_request_secure``."""
    req = MagicMock()
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = peer
    req.url = MagicMock()
    req.url.scheme = scheme
    return req


class TestIsRequestSecure:
    def test_default_is_secure(self, monkeypatch):
        """Default behaviour is to report the request as secure.

        This is the intended production default — cookies ship with
        Secure=True unless the operator explicitly opts out for a
        dev/loopback deployment.
        """
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)

        req = _request(headers={}, peer="203.0.113.99", scheme="http")
        assert is_request_secure(req) is True

    def test_force_false_opt_out(self, monkeypatch):
        """Operator can force-off Secure cookies for local dev."""
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "false")
        req = _request(headers={}, peer="127.0.0.1", scheme="http")
        assert is_request_secure(req) is False

    def test_force_true_override(self, monkeypatch):
        """MEDIAMAN_FORCE_SECURE_COOKIES=true wins regardless of headers."""
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "true")
        req = _request(headers={}, peer="203.0.113.99", scheme="http")
        assert is_request_secure(req) is True

    def test_direct_https_request(self, monkeypatch):
        """Native HTTPS request is secure."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)

        req = _request(headers={}, peer="203.0.113.99", scheme="https")
        assert is_request_secure(req) is True

    def test_trusted_proxy_header_honoured(self, monkeypatch):
        """Trusted-peer X-Forwarded-Proto still honoured (belt-and-braces)."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        req = _request(
            headers={"x-forwarded-proto": "https"},
            peer="10.1.2.3",
            scheme="http",
        )
        assert is_request_secure(req) is True

    def test_spoofed_header_without_trusted_proxy_still_secure_by_default(
        self, monkeypatch
    ):
        """Default-secure: attacker spoofing XFP without being a trusted peer
        doesn't downgrade the cookie — the default is already secure."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)

        req = _request(
            headers={"x-forwarded-proto": "https"},
            peer="203.0.113.99",
            scheme="http",
        )
        assert is_request_secure(req) is True
