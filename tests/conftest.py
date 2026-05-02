"""Shared test fixtures."""

import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Tests directory root, used by ``pytest_collection_modifyitems`` to attach
# unit/integration markers based on which subtree a test file lives under.
_TESTS_ROOT = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    """Auto-apply ``unit`` / ``integration`` markers based on test path.

    The pyproject declares both markers under ``[tool.pytest.ini_options]``
    but every test file would otherwise need to opt in manually. We tag
    everything under ``tests/unit/`` with ``@pytest.mark.unit`` and anything
    under ``tests/integration/`` with ``@pytest.mark.integration`` so
    selecting a subset via ``-m unit`` or ``-m integration`` works without
    editing every test.

    Tests that explicitly mark themselves keep their existing marker — we
    only add when missing. Tests outside both subtrees are left unmarked
    so the strict-markers config doesn't trip on ad-hoc placements.
    """
    unit_root = _TESTS_ROOT / "unit"
    integration_root = _TESTS_ROOT / "integration"
    for item in items:
        try:
            item_path = Path(item.path).resolve()
        except Exception:
            continue
        if unit_root in item_path.parents:
            if "unit" not in {m.name for m in item.iter_markers()}:
                item.add_marker(pytest.mark.unit)
        elif integration_root in item_path.parents and "integration" not in {
            m.name for m in item.iter_markers()
        }:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def _fake_dns_ok(monkeypatch):
    """Make every hostname resolve to a benign public IP by default.

    The SSRF guard now refuses hostnames that fail DNS resolution.
    Unit tests run without network, so every URL would be refused
    unless we stub resolution. Tests that want to check the guard's
    resolution logic itself override this via their own monkeypatch.
    """

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _clear_scanner_runner_caches():
    """Reset module-level scanner caches between tests.

    The runner caches a constructed ``PlexClient`` keyed on the stored
    Plex settings hash so the hot ``run_library_sync`` path doesn't
    rebuild it every 30 minutes. In tests, that cache leaks across
    cases (e.g. test 1 builds a real client; test 2 patches
    ``_build_plex`` to raise but the cached client from test 1 short-
    circuits the patch). Always-clear is the safest default.
    """
    try:
        from mediaman.scanner.runner import _reset_plex_client_cache
    except Exception:
        return
    _reset_plex_client_cache()
    yield
    _reset_plex_client_cache()


@pytest.fixture(autouse=True)
def _reset_db_connection_state():
    """Drop any thread-local DB connection registered by the previous test.

    ``set_connection`` writes module-level globals
    (``_owning_conn``/``_owning_thread``) and a thread-local
    ``_thread_local.conn``. Without an explicit reset between tests, a
    connection bound to a SQLite file from test A keeps responding to
    ``get_db()`` calls from test B — and because pytest's tmp_path
    fixtures change the file underneath, the stale handle silently
    returns rows from the wrong DB.
    """
    yield
    try:
        from mediaman.db import reset_connection
    except Exception:
        return
    reset_connection()


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
        raise AssertionError(f"No response queued for {method} {url} in test")


def _fake_response(*, status=200, json_data=None, text="", content=None, headers=None):
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
    resp.json = lambda: (
        json_data if json_data is not None else __import__("json").loads(content.decode())
    )
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
    from mediaman.services.infra import http_client

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
