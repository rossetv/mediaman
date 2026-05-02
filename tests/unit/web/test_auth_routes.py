"""Tests for :mod:`mediaman.web.routes.auth_routes` helpers.

``is_request_secure`` now defaults to True on a public deployment
(the common case); operators who genuinely need plaintext cookies in
local dev can set ``MEDIAMAN_FORCE_SECURE_COOKIES=false``. This is
the opposite of the previous default — accepting the previous
"fail-open to plaintext" behaviour was the root cause of the cookie
downgrade bug found in the security audit.
"""

from unittest.mock import MagicMock

import pytest

from mediaman.web.routes.auth import (
    _sanitise_log_field,
    _secure_cookie_override,
    _ua_hash,
    is_request_secure,
)


def _request(headers=None, peer="203.0.113.99", scheme="http"):
    """Build a minimal FastAPI Request-alike for ``is_request_secure``."""
    req = MagicMock()
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = peer
    req.url = MagicMock()
    req.url.scheme = scheme
    return req


@pytest.fixture(autouse=True)
def _reset_secure_cookie_cache():
    """``_secure_cookie_override`` is module-scope ``lru_cache``-d.

    Tests that mutate ``MEDIAMAN_FORCE_SECURE_COOKIES`` mid-process must
    invalidate it on the way in AND on the way out, otherwise a value set
    by an earlier case bleeds into the next.
    """
    _secure_cookie_override.cache_clear()
    yield
    _secure_cookie_override.cache_clear()


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

    def test_spoofed_header_without_trusted_proxy_still_secure_by_default(self, monkeypatch):
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


class TestSanitiseLogField:
    """_sanitise_log_field must strip control chars and truncate to prevent log injection."""

    def test_clean_username_unchanged(self):
        assert _sanitise_log_field("admin") == "admin"

    def test_strips_crlf(self):
        result = _sanitise_log_field("evil\r\nX-Injected: header")
        assert "\r" not in result
        assert "\n" not in result

    def test_strips_control_chars(self):
        result = _sanitise_log_field("user\x00name\x1b[31m")
        assert "\x00" not in result
        assert "\x1b" not in result

    def test_truncates_long_value(self):
        long_name = "a" * 100
        result = _sanitise_log_field(long_name, limit=64)
        assert result.endswith("...")
        # The sanitised body before the ellipsis is at most `limit` chars.
        assert len(result) <= 64 + 3

    def test_truncation_marker_present_when_truncated(self):
        result = _sanitise_log_field("a" * 65, limit=64)
        assert result.endswith("...")

    def test_no_marker_when_not_truncated(self):
        result = _sanitise_log_field("short", limit=64)
        assert not result.endswith("...")

    def test_email_style_username_preserved(self):
        result = _sanitise_log_field("user.name@example.com")
        assert result == "user.name@example.com"


class TestUaHash:
    """``_ua_hash`` must return a stable, length-bounded SHA-256 prefix."""

    def test_known_value(self):
        # Spot-check stability: SHA-256 of "" prefix.
        import hashlib

        expected = hashlib.sha256(b"").hexdigest()[:16]
        assert _ua_hash("") == expected

    def test_length_is_16_chars(self):
        assert len(_ua_hash("Mozilla/5.0")) == 16

    def test_long_input_does_not_pollute_output(self):
        # Previous implementation stored ``user_agent[:80]`` verbatim — a
        # 1MB UA would land 80 chars of attacker text in the audit blob.
        # The hash output is fixed-length regardless of input size.
        out = _ua_hash("X" * 1_000_000)
        assert len(out) == 16
        # Hex chars only — no attacker-controlled content can leak through.
        assert all(c in "0123456789abcdef" for c in out)

    def test_hash_is_deterministic(self):
        ua = "Mozilla/5.0 (compatible; test/1.0)"
        assert _ua_hash(ua) == _ua_hash(ua)


class TestSecureCookieOverrideCache:
    """The env-var read is cached so it's not re-evaluated on every request."""

    def test_cache_is_used(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "true")
        first = _secure_cookie_override()
        # Mutate the env without clearing the cache — second call should
        # still see the old value.
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "false")
        second = _secure_cookie_override()
        assert first == second == "true"

    def test_cache_clear_picks_up_new_value(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "true")
        assert _secure_cookie_override() == "true"
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "false")
        _secure_cookie_override.cache_clear()
        assert _secure_cookie_override() == "false"

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        assert _secure_cookie_override() is None

    def test_garbage_value_returns_none(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "yes-please")
        assert _secure_cookie_override() is None
