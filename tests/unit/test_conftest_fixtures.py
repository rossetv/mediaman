"""The suite-wide autouse fixtures hold up when what they reset cannot be imported."""

from __future__ import annotations

import contextlib
import socket
import sys

import pytest

from tests import conftest


def test_the_scanner_cache_reset_still_yields_when_the_scanner_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "mediaman.scanner.runner", None)
    fixture = conftest._clear_scanner_runner_caches.__wrapped__()

    # Without a yield every test would error with "did not yield a value".
    next(fixture)
    with pytest.raises(StopIteration):
        next(fixture)


def test_a_connection_out_of_this_machine_is_refused_and_noted_even_when_swallowed(_no_network):
    with contextlib.suppress(OSError):
        socket.create_connection(("api.example.com", 443), timeout=0.2)
    assert _no_network == [("93.184.216.34", 0)]
    _no_network.clear()


def test_a_connection_to_this_machine_is_allowed(_no_network):
    with socket.socket() as server, socket.socket() as client:
        server.bind(("127.0.0.1", 0))
        server.listen()
        client.connect(server.getsockname())
    assert _no_network == []
