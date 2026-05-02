"""Tests for login rate limiting."""

import logging
import threading
import time

import pytest

from mediaman.auth.rate_limit import (
    RateLimiter,
    get_client_ip,
    trusted_proxies,
)
from mediaman.auth.rate_limit import ip_resolver as ip_resolver_module
from mediaman.auth.rate_limit.ip_resolver import (
    clear_cache,
    cloudflare_proxies,
)


@pytest.fixture(autouse=True)
def _clear_proxy_cache():
    """The proxy-env parsers are LRU-cached; flush before and after each test
    so a previous test's environment doesn't leak into the next."""
    clear_cache()
    yield
    clear_cache()


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


class TestGetClientIpH7:
    """H7: focused test matrix for multi-hop XFF chains and X-Real-IP validation."""

    # ------------------------------------------------------------------ H7(a)
    def test_chain_only_innermost_proxy_trusted(self, monkeypatch):
        """Proxy chain A → B → client where only B is trusted.

        XFF: attacker, real, B — only B is in MEDIAMAN_TRUSTED_PROXIES.
        Must return ``real`` (first untrusted right-to-left), not
        ``attacker`` (the leftmost, attacker-controlled entry).
        """
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.2")

        class FakeRequest:
            headers = {"x-forwarded-for": "203.0.113.1, 198.51.100.7, 10.0.0.2"}
            client = type("C", (), {"host": "10.0.0.2"})()

        # 10.0.0.2 is trusted (peer == proxy B), 198.51.100.7 is real client,
        # 203.0.113.1 is attacker-supplied — must NOT be returned.
        result = get_client_ip(FakeRequest())
        assert result == "198.51.100.7"

    # ------------------------------------------------------------------ H7(b)
    def test_untrusted_proxy_xff_is_ignored(self, monkeypatch):
        """An untrusted proxy's XFF header must not be honoured.

        Even if XFF looks plausible, the peer is not in the trusted list,
        so we fall back to the peer address.
        """
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
            client = type("C", (), {"host": "203.0.113.99"})()  # NOT in 10/8

        assert get_client_ip(FakeRequest()) == "203.0.113.99"

    # ------------------------------------------------------------------ H7(c)
    def test_malformed_x_real_ip_falls_through_to_peer(self, monkeypatch):
        """A non-IP X-Real-IP must be silently discarded; peer is returned."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-real-ip": "not-an-ip-address"}
            client = type("C", (), {"host": "10.0.0.1"})()

        # Malformed X-Real-IP — must NOT be returned. Peer is trusted but
        # no valid forwarded IP is available, so return the peer itself.
        assert get_client_ip(FakeRequest()) == "10.0.0.1"

    def test_valid_x_real_ip_is_accepted(self, monkeypatch):
        """Sanity: a well-formed X-Real-IP from a trusted proxy is accepted."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-real-ip": "198.51.100.42"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "198.51.100.42"

    # ------------------------------------------------------------------ H7(d)
    def test_empty_xff_falls_through_to_peer(self, monkeypatch):
        """An empty (or absent) XFF header must fall through to peer."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-forwarded-for": ""}
            client = type("C", (), {"host": "10.0.0.1"})()

        # No XFF entries, no X-Real-IP — trusted peer itself is returned.
        assert get_client_ip(FakeRequest()) == "10.0.0.1"

    def test_all_xff_entries_trusted_falls_through_to_peer_with_warning(self, monkeypatch, caplog):
        """When every XFF entry is a trusted proxy, fall back to the direct peer.

        The fall-through is also logged as a warning because it usually
        indicates a misconfigured trusted-proxy list (the operator has
        listed every internal hop including the client's own subnet, or
        a proxy is double-counting itself).
        """
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-forwarded-for": "10.0.0.3, 10.0.0.2, 10.0.0.1"}
            client = type("C", (), {"host": "10.0.0.1"})()

        with caplog.at_level(logging.WARNING, logger=ip_resolver_module.__name__):
            assert get_client_ip(FakeRequest()) == "10.0.0.1"

        # Warning about the fully-trusted chain must be present.
        msgs = [r.message for r in caplog.records]
        assert any("no untrusted entries" in m for m in msgs), msgs


class TestGetClientIpCloudflare:
    """``cf-connecting-ip`` is honoured ONLY when the peer is in the
    *Cloudflare* allowlist, not the general trusted-proxy list."""

    def test_cf_connecting_ip_honoured_when_peer_is_cloudflare(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        monkeypatch.setenv("MEDIAMAN_CLOUDFLARE_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"cf-connecting-ip": "198.51.100.5"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "198.51.100.5"

    def test_cf_connecting_ip_ignored_when_peer_not_cloudflare(self, monkeypatch):
        """Trusted peer that is NOT in the Cloudflare list must NOT honour cf-connecting-ip.

        Otherwise any trusted proxy could spoof arbitrary client IPs by
        sending a forged ``cf-connecting-ip`` header.
        """
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        # Cloudflare list is a DIFFERENT subnet — the peer is in the
        # generic trusted list but NOT in the Cloudflare list.
        monkeypatch.setenv("MEDIAMAN_CLOUDFLARE_PROXIES", "172.16.0.0/12")

        class FakeRequest:
            # Attacker (the trusted proxy itself) attempts to spoof a
            # client IP via cf-connecting-ip. They also send an XFF so
            # the function has something to fall through to.
            headers = {
                "cf-connecting-ip": "1.1.1.1",
                "x-forwarded-for": "198.51.100.7, 10.0.0.1",
            }
            client = type("C", (), {"host": "10.0.0.1"})()

        # 1.1.1.1 must be IGNORED. XFF resolves the real client.
        assert get_client_ip(FakeRequest()) == "198.51.100.7"

    def test_cf_connecting_ip_ignored_when_cloudflare_list_empty(self, monkeypatch):
        """Empty MEDIAMAN_CLOUDFLARE_PROXIES → cf-connecting-ip is never honoured."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        monkeypatch.delenv("MEDIAMAN_CLOUDFLARE_PROXIES", raising=False)

        class FakeRequest:
            headers = {
                "cf-connecting-ip": "1.1.1.1",
                "x-forwarded-for": "198.51.100.7, 10.0.0.1",
            }
            client = type("C", (), {"host": "10.0.0.1"})()

        # cf-connecting-ip MUST NOT be trusted with no CF list configured.
        assert get_client_ip(FakeRequest()) == "198.51.100.7"

    def test_cf_connecting_ip_ignored_when_peer_untrusted(self, monkeypatch):
        """A peer not in MEDIAMAN_TRUSTED_PROXIES short-circuits before CF check."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        monkeypatch.setenv("MEDIAMAN_CLOUDFLARE_PROXIES", "203.0.113.0/24")

        class FakeRequest:
            headers = {"cf-connecting-ip": "1.1.1.1"}
            client = type("C", (), {"host": "192.0.2.5"})()  # not in trusted

        # Peer untrusted → return peer, never look at cf-connecting-ip.
        assert get_client_ip(FakeRequest()) == "192.0.2.5"


class TestGetClientIpXffValidation:
    """X-Forwarded-For entries must each parse as IPs; non-IPs are skipped+logged."""

    def test_non_ip_xff_entry_is_skipped(self, monkeypatch, caplog):
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            # First entry is rubbish. Right-to-left walk skips 10.0.0.1
            # (trusted) and 'garbage' (non-IP), then returns the real IP.
            headers = {"x-forwarded-for": "garbage, 198.51.100.7, 10.0.0.1"}
            client = type("C", (), {"host": "10.0.0.1"})()

        with caplog.at_level(logging.WARNING, logger=ip_resolver_module.__name__):
            result = get_client_ip(FakeRequest())

        assert result == "198.51.100.7"
        msgs = [r.message for r in caplog.records]
        assert any("non-IP entry" in m for m in msgs), msgs

    def test_xff_with_only_non_ip_entries_falls_through(self, monkeypatch, caplog):
        """If every XFF entry is invalid, fall back to peer (and warn)."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")

        class FakeRequest:
            headers = {"x-forwarded-for": "junk, more-junk"}
            client = type("C", (), {"host": "10.0.0.1"})()

        with caplog.at_level(logging.WARNING, logger=ip_resolver_module.__name__):
            assert get_client_ip(FakeRequest()) == "10.0.0.1"


class TestTrustedProxiesParser:
    """Parser-level guarantees: wildcard rejection, caching, logging."""

    def test_wildcard_rejected_with_critical_log(self, monkeypatch, caplog):
        """``MEDIAMAN_TRUSTED_PROXIES='*'`` → empty list + CRITICAL log.

        Even if main.py forwards ``*`` to uvicorn (which would let the
        peer header itself be spoofed), mediaman's own rate-limit checks
        must NOT trust the spoofed peer header.
        """
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "*")

        with caplog.at_level(logging.CRITICAL, logger=ip_resolver_module.__name__):
            result = trusted_proxies()

        assert result == []
        critical_msgs = [r.message for r in caplog.records if r.levelno >= logging.CRITICAL]
        assert any("wildcard" in m.lower() for m in critical_msgs), critical_msgs

    def test_invalid_cidr_logged_as_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8,not-a-cidr,192.168.0.0/16")

        with caplog.at_level(logging.WARNING, logger=ip_resolver_module.__name__):
            result = trusted_proxies()

        # Valid entries kept, invalid one logged + skipped.
        assert len(result) == 2
        warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("not-a-cidr" in m for m in warnings), warnings

    def test_result_is_cached(self, monkeypatch):
        """The parser is LRU-cached; second call without clear_cache()
        returns the same parsed value even if the env var changed."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        first = trusted_proxies()

        # Mutate the env var WITHOUT clearing the cache.
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "192.168.0.0/16")
        second = trusted_proxies()
        assert first == second  # cached

        # Now flush — next call sees the new value.
        clear_cache()
        third = trusted_proxies()
        assert third != first

    def test_cloudflare_proxies_default_empty(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_CLOUDFLARE_PROXIES", raising=False)
        assert cloudflare_proxies() == []

    def test_cloudflare_proxies_wildcard_rejected(self, monkeypatch, caplog):
        monkeypatch.setenv("MEDIAMAN_CLOUDFLARE_PROXIES", "*")

        with caplog.at_level(logging.CRITICAL, logger=ip_resolver_module.__name__):
            assert cloudflare_proxies() == []
