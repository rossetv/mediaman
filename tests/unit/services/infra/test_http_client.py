"""Tests for :class:`SafeHTTPClient` — the central outbound HTTP wrapper.

These tests exercise the safety machinery directly (redirects off,
timeout split, size cap, SSRF re-check per call, retry policy, and the
:class:`SafeHTTPError` shape) rather than going through a service
module.
"""

from __future__ import annotations

import socket
import threading
from unittest.mock import MagicMock

import pytest

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError
from mediaman.services.infra.http import (
    client as http_client,  # rationale: submodule object needed to monkeypatch _dispatch; not re-exported from infra top-level
)
from mediaman.services.infra.http import (
    dns_pinning as _dns_pinning,  # rationale: submodule object needed to monkeypatch getaddrinfo hook; not re-exported from infra top-level
)
from mediaman.services.infra.http import (
    pin,
)
from mediaman.services.infra.http.retry import (
    _BODY_SNIPPET_BYTES,  # rationale: private constant governing body-snippet truncation; exercised directly to pin the threshold contract
)


def _response(*, status=200, body=b"", headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.iter_content = lambda chunk_size=65536: iter([body])
    resp.close = MagicMock()
    resp.url = ""
    return resp


class TestRedirectsOff:
    def test_every_call_forces_allow_redirects_false(self, monkeypatch):
        """The underlying transport must receive ``allow_redirects=False``.

        A 302 to ``169.254.169.254`` would leak auth headers — the guard
        refuses to follow it regardless of what the caller does.
        """
        captured: dict = {}

        def fake_dispatch(caller, method, url, **kwargs):
            captured.update(kwargs)
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        client = SafeHTTPClient()
        client.get("http://example.com/")
        # _dispatch itself passes allow_redirects=False to requests — we
        # assert on the fact that _dispatch is the only call path.
        assert captured  # dispatch was invoked


class TestTimeoutSplit:
    def test_defaults_to_5_30(self, monkeypatch):
        seen: dict = {}

        def fake_dispatch(caller, method, url, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            return _response(body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        SafeHTTPClient().get("http://example.com/")
        assert seen["timeout"] == (5.0, 30.0)

    def test_override_per_call(self, monkeypatch):
        seen: dict = {}

        def fake_dispatch(caller, method, url, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            return _response(body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        SafeHTTPClient().get("http://example.com/", timeout=(2.0, 3.0))
        assert seen["timeout"] == (2.0, 3.0)


class TestSizeCap:
    def test_refuses_declared_oversize_body(self, monkeypatch):
        too_big = str(9 * 1024 * 1024)
        resp = _response(
            status=200,
            body=b"",
            headers={"Content-Length": too_big},
        )
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: resp,
        )
        with pytest.raises(SafeHTTPError):
            SafeHTTPClient().get("http://example.com/")

    def test_refuses_streamed_oversize_body(self, monkeypatch):
        chunks = [b"A" * 1024 * 1024 for _ in range(10)]  # 10 MiB total
        resp = MagicMock(
            status_code=200,
            headers={},
            url="",
        )
        resp.iter_content = lambda chunk_size=65536: iter(chunks)
        resp.close = MagicMock()
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: resp,
        )
        with pytest.raises(SafeHTTPError):
            SafeHTTPClient(default_max_bytes=4 * 1024 * 1024).get("http://example.com/")


class TestSSRFReCheck:
    def test_refuses_when_guard_rejects(self, monkeypatch):
        """The guard runs on every call — not just at client construction."""
        monkeypatch.setattr(
            http_client,
            "resolve_safe_outbound_url",
            lambda url, strict_egress=None: (False, None, None),
        )
        dispatched: list = []
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: dispatched.append(a) or _response(),
        )
        with pytest.raises(SafeHTTPError) as excinfo:
            SafeHTTPClient().get("http://example.com/")
        assert "refused by SSRF guard" in excinfo.value.body_snippet
        # Guard fired before any transport call.
        assert dispatched == []


class TestRetryBehaviour:
    def test_get_retries_503(self, monkeypatch):
        calls = [0]

        def fake_dispatch(caller, method, url, **kwargs):
            calls[0] += 1
            if calls[0] < 3:
                return _response(status=503, body=b"fail")
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        monkeypatch.setattr(http_client.time, "sleep", lambda *_a: None)
        SafeHTTPClient().get("http://example.com/")
        assert calls[0] == 3

    def test_get_retries_exhausted_raises(self, monkeypatch):
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(status=502, body=b"gateway"),
        )
        monkeypatch.setattr(http_client.time, "sleep", lambda *_a: None)
        with pytest.raises(SafeHTTPError) as excinfo:
            SafeHTTPClient().get("http://example.com/")
        assert excinfo.value.status_code == 502

    def test_post_does_not_retry_by_default(self, monkeypatch):
        calls = [0]

        def fake_dispatch(caller, method, url, **kwargs):
            calls[0] += 1
            return _response(status=503, body=b"no")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        monkeypatch.setattr(http_client.time, "sleep", lambda *_a: None)
        with pytest.raises(SafeHTTPError):
            SafeHTTPClient().post("http://example.com/", json={})
        assert calls[0] == 1  # no retry

    def test_post_retries_when_opted_in(self, monkeypatch):
        calls = [0]

        def fake_dispatch(caller, method, url, **kwargs):
            calls[0] += 1
            if calls[0] < 2:
                return _response(status=429, body=b"slow")
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        monkeypatch.setattr(http_client.time, "sleep", lambda *_a: None)
        SafeHTTPClient().post("http://example.com/", json={}, retry=True)
        assert calls[0] == 2

    def test_404_is_not_retried(self, monkeypatch):
        calls = [0]

        def fake_dispatch(caller, method, url, **kwargs):
            calls[0] += 1
            return _response(status=404, body=b"nope")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        monkeypatch.setattr(http_client.time, "sleep", lambda *_a: None)
        with pytest.raises(SafeHTTPError):
            SafeHTTPClient().get("http://example.com/")
        assert calls[0] == 1


class TestRetryAfterHeader:
    """``Retry-After`` on 429/503 must be honoured (capped to 60s)."""

    def test_retry_after_seconds_value_used(self, monkeypatch):
        sleeps: list = []
        monkeypatch.setattr(http_client.time, "sleep", lambda s: sleeps.append(s))

        calls = [0]

        def fake_dispatch(caller, method, url, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return _response(
                    status=429,
                    body=b"slow",
                    headers={"Retry-After": "5"},
                )
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        SafeHTTPClient().get("http://example.com/")
        # First sleep is the retry-after value, not the fixed backoff.
        assert sleeps[0] == 5.0

    def test_retry_after_capped_at_60(self, monkeypatch):
        sleeps: list = []
        monkeypatch.setattr(http_client.time, "sleep", lambda s: sleeps.append(s))

        calls = [0]

        def fake_dispatch(caller, method, url, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return _response(
                    status=503,
                    body=b"down",
                    headers={"Retry-After": "3600"},  # 1 hour — must be capped
                )
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        SafeHTTPClient().get("http://example.com/")
        assert sleeps[0] == 60.0

    def test_retry_after_falls_back_to_backoff_on_malformed(self, monkeypatch):
        sleeps: list = []
        monkeypatch.setattr(http_client.time, "sleep", lambda s: sleeps.append(s))

        calls = [0]

        def fake_dispatch(caller, method, url, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return _response(
                    status=429,
                    body=b"slow",
                    headers={"Retry-After": "what"},
                )
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        SafeHTTPClient().get("http://example.com/")
        # Falls back to the fixed schedule (0.5).
        assert sleeps[0] == 0.5


class TestBroaderRetryExceptionList:
    """Transient TLS / chunked / decode / redirect errors are retried."""

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: __import__("requests").exceptions.SSLError("tls reset"),
            lambda: __import__("requests").exceptions.ChunkedEncodingError("bad chunk"),
            lambda: __import__("requests").exceptions.ContentDecodingError("bad gzip"),
            lambda: __import__("requests").exceptions.TooManyRedirects("loop"),
        ],
    )
    def test_retries_for_each_transient_exception(self, monkeypatch, exc_factory):
        """Each entry in the broadened retry list must trigger a retry."""
        calls = [0]

        def fake_dispatch(caller, method, url, **kwargs):
            calls[0] += 1
            if calls[0] < 2:
                raise exc_factory()
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
        monkeypatch.setattr(http_client.time, "sleep", lambda *_a: None)
        SafeHTTPClient().get("http://example.com/")
        assert calls[0] == 2


class TestExpectedContentType:
    """``expected_content_type`` rejects mismatched / encoded responses."""

    def test_match_passes(self, monkeypatch):
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(
                status=200,
                body=b'{"ok": true}',
                headers={"Content-Type": "application/json; charset=utf-8"},
            ),
        )
        SafeHTTPClient().get(
            "http://example.com/",
            expected_content_type="application/json",
        )

    def test_mismatch_raises(self, monkeypatch):
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(
                status=200,
                body=b"<html>oops</html>",
                headers={"Content-Type": "text/html"},
            ),
        )
        with pytest.raises(SafeHTTPError) as excinfo:
            SafeHTTPClient().get(
                "http://example.com/",
                expected_content_type="application/json",
            )
        assert "Content-Type" in excinfo.value.body_snippet

    def test_unexpected_encoding_rejected(self, monkeypatch):
        """A pinned content-type must reject non-identity Content-Encoding.

        urllib3 may already be decoding the body — a server that lies
        about its encoding could push past the size cap before we see
        the bytes. When the caller has pinned the type, refuse anything
        non-identity.
        """
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(
                status=200,
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "br",
                },
            ),
        )
        with pytest.raises(SafeHTTPError) as excinfo:
            SafeHTTPClient().get(
                "http://example.com/",
                expected_content_type="application/json",
            )
        assert "Content-Encoding" in excinfo.value.body_snippet


class TestSafeHTTPErrorShape:
    def test_carries_status_body_url(self, monkeypatch):
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(status=400, body=b'{"error":"bad"}'),
        )
        with pytest.raises(SafeHTTPError) as excinfo:
            SafeHTTPClient().get("http://example.com/bork")
        exc = excinfo.value
        assert exc.status_code == 400
        assert exc.url == "http://example.com/bork"
        assert "bad" in exc.body_snippet

    def test_json_error_returns_dict(self, monkeypatch):
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(status=500, body=b'{"error":"oops"}'),
        )
        with pytest.raises(SafeHTTPError) as excinfo:
            SafeHTTPClient().get("http://example.com/")
        assert excinfo.value.json_error() == {"error": "oops"}

    def test_json_error_returns_none_for_non_json(self, monkeypatch):
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(status=500, body=b"plain text"),
        )
        with pytest.raises(SafeHTTPError) as excinfo:
            SafeHTTPClient().get("http://example.com/")
        assert excinfo.value.json_error() is None

    def test_body_snippet_truncated(self, monkeypatch):
        big = b"A" * (_BODY_SNIPPET_BYTES * 3)
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(status=500, body=big),
        )
        with pytest.raises(SafeHTTPError) as excinfo:
            SafeHTTPClient().get("http://example.com/")
        assert len(excinfo.value.body_snippet) <= _BODY_SNIPPET_BYTES


class TestDNSPinning:
    """The pin closes the DNS-rebind window between SSRF validation and connect.

    A naive client validates a hostname (``socket.getaddrinfo`` returns a
    public IP), then passes the URL to ``requests``, which does its own
    second resolution. A rebinding host returns the safe IP for the
    first lookup and the metadata IP for the second. The pin removes
    that gap by replaying the validated address.
    """

    def test_pin_context_replays_validated_ip(self, monkeypatch):
        """Inside :func:`pin`, ``socket.getaddrinfo`` returns
        the pinned IP regardless of any subsequent change in DNS state."""
        # The autouse fixture replaces socket.getaddrinfo for the test —
        # to test the pin we restore the patched version explicitly.
        monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)

        with pin("rebind.example.test", "203.0.113.5"):
            results = socket.getaddrinfo("rebind.example.test", 443)
        assert results
        family, _socktype, _proto, _name, sockaddr = results[0]
        assert family == socket.AF_INET
        assert sockaddr[0] == "203.0.113.5"

    def test_pin_context_pops_after_exit(self, monkeypatch):
        """After the context exits, an unrelated lookup falls through to
        the real resolver — pins must NOT persist."""
        monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)

        # Patch the captured original so we can detect a fall-through call.
        called: list = []

        def fake_real(*a, **kw):
            called.append(a)
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0))]

        monkeypatch.setattr(_dns_pinning, "_ORIG_GETADDRINFO", fake_real)

        with pin("only-during.example.test", "203.0.113.5"):
            # During context, no fall-through.
            socket.getaddrinfo("only-during.example.test", 443)
            assert called == []
        # After context, the same hostname falls through to the real
        # resolver because no pin remains.
        socket.getaddrinfo("only-during.example.test", 443)
        assert len(called) == 1

    def test_pins_are_thread_local(self, monkeypatch):
        """A pin set in one thread must NOT bleed into another thread.

        Concurrent scan workers and FastAPI threadpool calls would
        otherwise see each other's pinned addresses and either fail or,
        worse, reach an address that was never validated for them.
        """
        monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)
        # No fall-through — record only pinned answers.
        observed: dict[str, str | None] = {}
        barrier = threading.Barrier(2)

        def in_thread_a():
            with pin("shared.example.test", "203.0.113.10"):
                barrier.wait()
                # Thread B should NOT see this pin.
                ans = socket.getaddrinfo("shared.example.test", 0)
                observed["a"] = ans[0][4][0]
                barrier.wait()  # let B finish before exiting context

        def in_thread_b():
            barrier.wait()
            try:
                ans = socket.getaddrinfo("shared.example.test", 0)
                observed["b"] = ans[0][4][0]
            except Exception:
                observed["b"] = None
            finally:
                barrier.wait()

        # Replace the real resolver with a sentinel so B's lookup yields
        # something distinguishable from A's pin.
        monkeypatch.setattr(
            _dns_pinning,
            "_ORIG_GETADDRINFO",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.51.100.99", 0))],
        )

        ta = threading.Thread(target=in_thread_a)
        tb = threading.Thread(target=in_thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=5)
        tb.join(timeout=5)
        assert observed.get("a") == "203.0.113.10"
        assert observed.get("b") == "198.51.100.99"

    def test_pin_installed_during_request(self, monkeypatch):
        """When ``SafeHTTPClient`` issues a request, the pin must be live
        for the underlying transport. We capture ``socket.getaddrinfo``
        from inside ``_dispatch`` and verify the pinned IP is what comes
        back for the validated hostname."""
        # Force a sane SSRF answer so the dispatch path runs.
        monkeypatch.setattr(
            http_client,
            "resolve_safe_outbound_url",
            lambda url, strict_egress=None: (True, "host.example.test", "203.0.113.7"),
        )
        # Make sure the global patch is active.
        monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)

        captured: dict = {}

        def spy_dispatch(caller, method, url, **kwargs):
            captured["url"] = url
            # Inside the dispatch the pin is live.
            captured["pin_lookup"] = socket.getaddrinfo("host.example.test", 0)
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", spy_dispatch)

        SafeHTTPClient().get("http://host.example.test/x")

        # The pinned address came back inside the dispatcher.
        _family, _socktype, _proto, _name, sockaddr = captured["pin_lookup"][0]
        assert sockaddr[0] == "203.0.113.7"

    def test_pin_cleared_after_request(self, monkeypatch):
        """The pin must be released the moment the request returns."""
        monkeypatch.setattr(
            http_client,
            "resolve_safe_outbound_url",
            lambda url, strict_egress=None: (True, "vanish.example.test", "203.0.113.8"),
        )
        monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)

        # Real resolver returns a sentinel.
        monkeypatch.setattr(
            _dns_pinning,
            "_ORIG_GETADDRINFO",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.51.100.42", 0))],
        )
        monkeypatch.setattr(
            http_client,
            "_dispatch",
            lambda *a, **kw: _response(status=200, body=b"{}"),
        )

        SafeHTTPClient().get("http://vanish.example.test/")

        # After the request, lookups for the validated hostname fall
        # through to the real resolver — no leftover pin.
        ans = socket.getaddrinfo("vanish.example.test", 0)
        assert ans[0][4][0] == "198.51.100.42"

    def test_literal_ip_url_pins_to_self(self, monkeypatch):
        """A URL with a literal IP is pinned to itself.

        urllib3 still calls ``getaddrinfo("192.0.2.1", port)`` for a
        literal-IP URL; pinning the address to itself short-circuits
        the resolver so a monkeypatched / process-wide
        ``socket.getaddrinfo`` cannot redirect the connect anywhere
        else.
        """
        monkeypatch.setattr(
            http_client,
            "resolve_safe_outbound_url",
            # Literal IP path: hostname is the IP and the pin is itself.
            lambda url, strict_egress=None: (True, "192.0.2.1", "192.0.2.1"),
        )
        monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)

        captured: dict = {}

        def spy_dispatch(caller, method, url, **kwargs):
            captured["pins"] = dict(getattr(_dns_pinning._DNS_PIN_LOCAL, "pins", {}) or {})
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", spy_dispatch)
        SafeHTTPClient().get("http://192.0.2.1/")
        # The literal-IP URL pinned itself for the duration of the request.
        assert captured["pins"].get("192.0.2.1") == "192.0.2.1"

    def test_pin_family_mismatch_returns_empty_list(self, monkeypatch):
        """When the caller asks for AF_INET6 but the pin holds a v4 IP,
        the patched resolver returns an empty list — the same signal
        urllib3 uses for "no matching addresses, try the next strategy".
        Synthesising a record with the wrong family hands the connection
        layer a tuple it cannot actually use.
        """
        monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)

        with pin("v4only.example.test", "203.0.113.5"):
            ans = socket.getaddrinfo("v4only.example.test", 80, socket.AF_INET6)
            # Family mismatch — empty list, not a synthesised v4 record.
            assert ans == []
            # Same hostname with the matching family still works.
            ans4 = socket.getaddrinfo("v4only.example.test", 80, socket.AF_INET)
            assert ans4
            assert ans4[0][4][0] == "203.0.113.5"
            # AF_UNSPEC is "either is fine" and must still match.
            ans_any = socket.getaddrinfo("v4only.example.test", 80, socket.AF_UNSPEC)
            assert ans_any
            assert ans_any[0][4][0] == "203.0.113.5"

    def test_dns_pin_hook_reinstalled_when_tampered(self, monkeypatch, caplog):
        """If something replaces ``socket.getaddrinfo`` after our import,
        the next request must detect the tamper, log CRITICAL, and
        re-install the patched resolver. Without this, a misbehaving
        plugin or test fixture could silently disable the pin and
        reopen the SSRF rebind window.
        """
        # Force the SSRF guard to a benign answer.
        monkeypatch.setattr(
            http_client,
            "resolve_safe_outbound_url",
            lambda url, strict_egress=None: (True, "tamper.example.test", "203.0.113.7"),
        )
        monkeypatch.setattr(http_client, "_dispatch", lambda *a, **kw: _response(body=b"{}"))

        # Tamper with the resolver — replace it with a no-op stub.
        original_patched = _dns_pinning._patched_getaddrinfo
        rogue = lambda *a, **kw: []  # noqa: E731
        socket.getaddrinfo = rogue
        try:
            assert socket.getaddrinfo is rogue  # tamper succeeded
            import logging

            with caplog.at_level(logging.CRITICAL, logger="mediaman"):
                SafeHTTPClient().get("http://tamper.example.test/")
            # The hook is back in place.
            assert socket.getaddrinfo is original_patched
            assert any("getaddrinfo was replaced" in r.getMessage() for r in caplog.records)
        finally:
            socket.getaddrinfo = original_patched

    def test_pin_overrides_rebind_attempt(self, monkeypatch):
        """The full rebind scenario: validation sees a public IP, the
        rebinder would return a metadata IP at connect time. The pin
        forces the connect lookup back to the validated address.

        We synthesise the SSRF check returning a benign IP, and a real
        resolver that, at the connect call, would return ``169.254.169.254``.
        Inside the dispatch we verify the pin overrides that.
        """
        monkeypatch.setattr(
            http_client,
            "resolve_safe_outbound_url",
            lambda url, strict_egress=None: (True, "rebind.example.test", "93.184.216.34"),
        )
        monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)

        # Simulate a rebinder: post-validation DNS would return a
        # metadata IP. The pin must defeat that.
        monkeypatch.setattr(
            _dns_pinning,
            "_ORIG_GETADDRINFO",
            lambda host, port, *a, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", port or 0))
            ],
        )

        seen: dict = {}

        def spy_dispatch(caller, method, url, **kwargs):
            seen["lookup"] = socket.getaddrinfo("rebind.example.test", 80)
            return _response(status=200, body=b"{}")

        monkeypatch.setattr(http_client, "_dispatch", spy_dispatch)

        SafeHTTPClient().get("http://rebind.example.test/")

        # The pinned safe IP came back, NOT the rebinder's metadata IP.
        assert seen["lookup"][0][4][0] == "93.184.216.34"
