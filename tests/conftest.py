"""Shared test fixtures."""

import os
import socket
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _fake_dns_ok(monkeypatch):
    """Make every hostname resolve to a benign public IP by default.

    The SSRF guard now refuses hostnames that fail DNS resolution.
    Unit tests run without network, so every URL would be refused
    unless we stub resolution. Tests that want to check the guard's
    resolution logic itself override this via their own monkeypatch.
    """
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
        ]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


class _FakeHTTPSession:
    """Captures HTTP calls for tests that used to patch ``requests.get/post/...``.

    Install via the :func:`fake_http` fixture. Tests configure queued
    responses (one per verb or per call) and assert on the captured
    arguments afterwards.
    """

    def __init__(self):
        self.calls = []  # list of (method, url, kwargs)
        self._responses = {"GET": [], "POST": [], "PUT": [], "DELETE": []}
        self._default = None
        self._raising = {"GET": None, "POST": None, "PUT": None, "DELETE": None}

    def queue(self, method: str, response) -> None:
        self._responses[method.upper()].append(response)

    def default(self, response) -> None:
        self._default = response

    def raise_on(self, method: str, exc: BaseException) -> None:
        self._raising[method.upper()] = exc

    def handler(self, fn) -> None:
        """Install a callable ``fn(method, url, **kwargs)`` that returns a response or raises."""
        self._handler = fn

    _handler = None

    def request(self, method, url, **kwargs):  # matches requests.Session.request
        self.calls.append((method.upper(), url, kwargs))
        if self._handler is not None:
            return self._handler(method.upper(), url, **kwargs)
        exc = self._raising.get(method.upper())
        if exc is not None:
            raise exc
        bucket = self._responses.get(method.upper(), [])
        if bucket:
            return bucket.pop(0)
        if self._default is not None:
            return self._default
        raise AssertionError(
            f"No response queued for {method} {url} in test"
        )


def _fake_response(
    *, status=200, json_data=None, text="", content=None, headers=None
):
    """Return a MagicMock shaped like a ``requests.Response``."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 400
    resp.headers = headers or {}
    if content is None:
        if json_data is not None:
            import json as _j
            content = _j.dumps(json_data).encode()
        else:
            content = text.encode() if isinstance(text, str) else (text or b"")
    resp.content = content
    resp.iter_content = lambda chunk_size=65536: iter([content])
    resp.json = lambda: json_data if json_data is not None else __import__("json").loads(content.decode())
    resp.close = MagicMock()
    resp.url = ""
    return resp


@pytest.fixture
def fake_http(monkeypatch):
    """Patch the :class:`SafeHTTPClient` transport for a single test.

    Returns a :class:`_FakeHTTPSession`. Tests queue responses per verb
    (``fh.queue('GET', _fake_response(...))``) or set a default
    (``fh.default(...)``). The underlying ``_dispatch`` helper is
    monkey-patched to route through the fake, so every outbound call
    from any SafeHTTPClient in-process is captured.
    """
    from mediaman.services import http_client

    fh = _FakeHTTPSession()

    def fake_dispatch(caller, method, url, **kwargs):
        return fh.request(method, url, **kwargs)

    monkeypatch.setattr(http_client, "_dispatch", fake_dispatch)
    return fh


# Expose the response helper as a fixture too so tests can build
# responses without importing from conftest.
@pytest.fixture
def fake_response():
    return _fake_response


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Provide a temporary data directory."""
    return tmp_path


@pytest.fixture
def db_path(tmp_data_dir):
    """Provide a temporary database path."""
    return tmp_data_dir / "mediaman.db"


@pytest.fixture
def secret_key():
    """Provide a strong test secret key (64 hex chars, ~256 bits).

    Deterministic so tests get reproducible HMAC outputs; passes the
    entropy check in :mod:`mediaman.config`.
    """
    return "0123456789abcdef" * 4  # 64 hex chars, 16 unique, test-stable
