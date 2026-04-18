"""Tests for login rate limiting."""

import threading
import time

import pytest

from mediaman.auth.rate_limit import RateLimiter, get_client_ip


class TestRateLimiter:
    def test_allows_under_limit(self):
        limiter = RateLimiter(max_attempts=5, window_seconds=60)
        for _ in range(5):
            assert limiter.check("192.168.1.1") is True

    def test_blocks_over_limit(self):
        limiter = RateLimiter(max_attempts=3, window_seconds=60)
        for _ in range(3):
            limiter.check("192.168.1.1")
        assert limiter.check("192.168.1.1") is False

    def test_different_ips_independent(self):
        """Addresses in DIFFERENT /24 networks do not share a bucket."""
        limiter = RateLimiter(max_attempts=1, window_seconds=60)
        limiter.check("192.168.1.1")
        # 192.168.2.2 is a different /24 to 192.168.1.x — independent bucket.
        assert limiter.check("192.168.2.2") is True

    def test_same_subnet_shares_bucket(self):
        """Addresses in the SAME /24 share a bucket — prevents IP-hop evasion."""
        limiter = RateLimiter(max_attempts=1, window_seconds=60)
        limiter.check("192.168.1.1")
        assert limiter.check("192.168.1.99") is False

    def test_ipv6_same_slash64_shares_bucket(self):
        """IPv6 addresses within the same /64 share a rate-limit bucket."""
        limiter = RateLimiter(max_attempts=1, window_seconds=60)
        limiter.check("2001:db8:1234:5678::1")
        assert limiter.check("2001:db8:1234:5678::ffff") is False

    def test_resets_after_window(self):
        limiter = RateLimiter(max_attempts=1, window_seconds=0.1)
        limiter.check("192.168.1.1")
        assert limiter.check("192.168.1.1") is False
        time.sleep(0.15)
        assert limiter.check("192.168.1.1") is True

    def test_concurrent_check_is_atomic(self):
        """Concurrent check() calls must not let attempts slip past the limit."""
        limiter = RateLimiter(max_attempts=5, window_seconds=60)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker() -> None:
            allowed = limiter.check("10.0.0.1")
            with results_lock:
                results.append(allowed)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 5


class TestGetClientIp:
    def test_ignores_forwarded_headers_when_no_trusted_proxy(self, monkeypatch):
        """By default no proxy is trusted — forwarded headers are ignored."""
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)

        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4", "x-real-ip": "5.6.7.8"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "10.0.0.1"

    def test_trusts_forwarded_header_from_configured_proxy(self, monkeypatch):
        """Walks XFF from the right, skipping trusted-proxy hops."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "1.2.3.4"

    def test_rejects_spoofed_leftmost_xff_entry(self, monkeypatch):
        """An attacker-controlled leftmost XFF entry must NOT be taken as the client IP."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        # Attacker (real IP 198.51.100.7) sends their own X-Forwarded-For
        # with a forged leftmost value. The trusted proxy appends its own
        # identity. Walking right-to-left, we skip 10.0.0.1 (trusted) and
        # should return 198.51.100.7, NOT the forged 1.2.3.4.
        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4, 198.51.100.7, 10.0.0.1"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "198.51.100.7"

    def test_trusts_x_real_ip_when_no_forwarded_for(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-real-ip": "5.6.7.8"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "5.6.7.8"

    def test_prefers_forwarded_for_over_real_ip(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4", "x-real-ip": "5.6.7.8"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "1.2.3.4"

    def test_falls_back_to_client_host_for_untrusted_peer(self, monkeypatch):
        """Spoofed forwarded headers from an untrusted peer must be ignored."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4"}
            client = type("C", (), {"host": "192.168.1.100"})()

        assert get_client_ip(FakeRequest()) == "192.168.1.100"

    def test_returns_unknown_when_no_client(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)

        class FakeRequest:
            headers = {}
            client = None

        assert get_client_ip(FakeRequest()) == "unknown"
