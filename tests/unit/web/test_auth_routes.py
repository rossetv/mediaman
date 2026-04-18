"""Tests for :mod:`mediaman.web.routes.auth_routes` helpers.

Focused on :func:`_is_request_secure` — it must only honour
``X-Forwarded-Proto`` from clients that are within the trusted-proxy
allow-list, otherwise an unauthenticated attacker can force
``Secure=False`` on the admin session cookie.
"""

from unittest.mock import MagicMock

from mediaman.web.routes.auth_routes import _is_request_secure


def _request(headers=None, peer="203.0.113.99", scheme="http"):
    """Build a minimal FastAPI Request-alike for ``_is_request_secure``."""
    req = MagicMock()
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = peer
    req.url = MagicMock()
    req.url.scheme = scheme
    return req


class TestIsRequestSecure:
    def test_untrusted_proxy_header_ignored(self, monkeypatch):
        """Untrusted peer setting X-Forwarded-Proto must NOT downgrade."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)

        req = _request(
            headers={"x-forwarded-proto": "https"},
            peer="203.0.113.99",
            scheme="http",
        )
        # Header claims HTTPS but peer is untrusted — fall back to url.scheme.
        assert _is_request_secure(req) is False

    def test_trusted_proxy_header_honoured(self, monkeypatch):
        """When the peer is inside MEDIAMAN_TRUSTED_PROXIES, the header is trusted."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        req = _request(
            headers={"x-forwarded-proto": "https"},
            peer="10.1.2.3",
            scheme="http",
        )
        assert _is_request_secure(req) is True

    def test_trusted_proxy_reports_http(self, monkeypatch):
        """Trusted proxy saying 'http' means the outer hop is http — not secure."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        req = _request(
            headers={"x-forwarded-proto": "http"},
            peer="10.1.2.3",
            scheme="https",
        )
        assert _is_request_secure(req) is False

    def test_force_secure_override(self, monkeypatch):
        """MEDIAMAN_FORCE_SECURE_COOKIES=true always wins, regardless of headers."""
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "true")
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)

        req = _request(headers={}, peer="203.0.113.99", scheme="http")
        assert _is_request_secure(req) is True

    def test_direct_https_request(self, monkeypatch):
        """Without a proxy, fall through to request.url.scheme."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)

        req = _request(headers={}, peer="203.0.113.99", scheme="https")
        assert _is_request_secure(req) is True

    def test_attacker_cannot_spoof_header_without_trusted_proxy(self, monkeypatch):
        """Set the trusted list but peer NOT within it — header still ignored."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        # Peer is public internet, not 10.0.0.0/8
        req = _request(
            headers={"x-forwarded-proto": "https"},
            peer="198.51.100.7",
            scheme="http",
        )
        assert _is_request_secure(req) is False
