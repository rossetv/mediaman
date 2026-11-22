"""A host that resolves to several validated addresses can be reached on any of them.

``localhost`` commonly resolves to ``::1`` first while the service listens
only on ``127.0.0.1``. The connection must fall through the validated
addresses in order, and never dial one that was not validated.
"""

from __future__ import annotations

import http.server
import socket
import threading

import pytest

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError
from mediaman.services.infra.http import (
    dns_pinning as _dns_pinning,  # rationale: submodule object needed to install the pin-aware resolver and its delegate
)
from mediaman.services.infra.http import pin
from mediaman.services.infra.url_safety import PinnedIP

_HOST = "multi.example.test"


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def v4_only_server():
    """An HTTP server listening on 127.0.0.1 only; yields its port."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def resolver(monkeypatch):
    """Route lookups through the pin-aware resolver over a canned DNS answer.

    Returns the list of hosts the canned resolver was asked for, so a test
    can see whether the host was looked up again at connect time.
    """
    lookups: list[str] = []
    answers: dict[str, list[str]] = {}

    def canned(host, port, *args, **kwargs):
        lookups.append(host)
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                (ip, port or 0, 0, 0) if ":" in ip else (ip, port or 0),
            )
            for ip in answers[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)
    monkeypatch.setattr(_dns_pinning, "_ORIG_GETADDRINFO", canned)
    return answers, lookups


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_connection_falls_through_to_next_validated_address(resolver, v4_only_server):
    answers, _lookups = resolver
    answers[_HOST] = ["::1", "127.0.0.1"]

    resp = SafeHTTPClient().get(f"http://{_HOST}:{v4_only_server}/", retry=False)

    assert resp.status_code == 200


def test_host_is_not_resolved_again_at_connect_time(resolver, v4_only_server):
    answers, lookups = resolver
    answers[_HOST] = ["::1", "127.0.0.1"]

    SafeHTTPClient().get(f"http://{_HOST}:{v4_only_server}/", retry=False)

    assert lookups.count(_HOST) == 1


def test_every_pinned_address_refusing_is_a_transport_error(resolver):
    answers, _lookups = resolver
    answers[_HOST] = ["::1", "127.0.0.1"]

    with pytest.raises(SafeHTTPError) as excinfo:
        SafeHTTPClient().get(f"http://{_HOST}:{_closed_port()}/", retry=False)

    assert excinfo.value.status_code == 0


def test_pin_offers_only_validated_addresses_in_order(resolver):
    answers, lookups = resolver
    answers[_HOST] = ["198.51.100.99"]

    with pin(_HOST, PinnedIP(["::1", "203.0.113.5", "203.0.113.6"])):
        any_family = socket.getaddrinfo(_HOST, 80, socket.AF_UNSPEC, socket.SOCK_STREAM)
        v4_only = socket.getaddrinfo(_HOST, 80, socket.AF_INET, socket.SOCK_STREAM)

    assert [info[4][0] for info in any_family] == ["::1", "203.0.113.5", "203.0.113.6"]
    assert [info[4][0] for info in v4_only] == ["203.0.113.5", "203.0.113.6"]
    assert lookups == []
