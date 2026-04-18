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
        limiter = RateLimiter(max_attempts=1, window_seconds=60)
        limiter.check("192.168.1.1")
        assert limiter.check("192.168.1.2") is True

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
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "1.2.3.4"

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
