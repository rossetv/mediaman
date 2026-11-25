"""The suite-wide autouse fixtures hold up when what they reset cannot be imported."""

from __future__ import annotations

import contextlib
import importlib
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


@pytest.mark.parametrize("host", ["192.0.2.1", "api.example.com"])
@pytest.mark.parametrize("how", ["connect", "connect_ex"])
def test_a_connection_out_is_refused_and_noted_even_when_swallowed(_no_network, how, host):
    with socket.socket() as client, contextlib.suppress(OSError):
        client.settimeout(0.1)
        getattr(client, how)((host, 443))
    assert _no_network == [(host, 443)]
    _no_network.clear()


@pytest.mark.parametrize(
    ("how", "args"), [("sendto", (b"x",)), ("sendto", (b"x", 0)), ("sendmsg", ([b"x"], [], 0))]
)
def test_a_datagram_out_is_refused_and_noted(_no_network, how, args):
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client,
        contextlib.suppress(OSError),
    ):
        getattr(client, how)(*args, ("192.0.2.1", 9))
    assert _no_network == [("192.0.2.1", 9)]
    _no_network.clear()


def test_undoing_every_patch_leaves_the_guard_and_the_fake_dns(_no_network, monkeypatch):
    monkeypatch.undo()
    with socket.socket() as client, contextlib.suppress(OSError):
        client.settimeout(0.1)
        client.connect(("192.0.2.1", 443))
    assert _no_network == [("192.0.2.1", 443)]
    _no_network.clear()
    assert socket.getaddrinfo("api.example.com", 443)[0][4] == ("93.184.216.34", 0)


@pytest.fixture(scope="module")
def module_wide():
    with socket.socket() as client, contextlib.suppress(OSError):
        client.settimeout(0.1)
        client.connect(("192.0.2.1", 443))
    return conftest._tried_network.copy(), socket.getaddrinfo("api.example.com", 443)


def test_a_fixture_of_a_wider_scope_is_guarded_too(module_wide, _no_network):
    tried, found = module_wide
    _no_network.clear()
    assert tried == [("192.0.2.1", 443)]
    assert found[0][4] == ("93.184.216.34", 0)


def test_what_was_tried_fails_whoever_is_checked_next(_no_network):
    _no_network.append(("192.0.2.1", 443))
    with pytest.raises(pytest.fail.Exception):
        conftest._fail_if_tried()
    assert _no_network == []


def test_uvloop_which_the_guard_cannot_see_is_not_used():
    with pytest.raises(ImportError):
        importlib.import_module("uvloop")


def test_a_connection_to_this_machine_is_allowed(_no_network):
    with socket.socket() as server, socket.socket() as client:
        server.bind(("127.0.0.1", 0))
        server.listen()
        client.connect(server.getsockname())
    assert _no_network == []
