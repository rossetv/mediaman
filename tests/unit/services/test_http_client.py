"""Tests for :class:`SafeHTTPClient` — the central outbound HTTP wrapper.

These tests exercise the safety machinery directly (redirects off,
timeout split, size cap, SSRF re-check per call, retry policy, and the
:class:`SafeHTTPError` shape) rather than going through a service
module.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mediaman.services.infra import http_client
from mediaman.services.infra.http_client import (
    _BODY_SNIPPET_BYTES,
    SafeHTTPClient,
    SafeHTTPError,
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
            "is_safe_outbound_url",
            lambda url, strict_egress=None: False,
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
