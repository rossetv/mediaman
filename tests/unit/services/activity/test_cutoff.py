"""A reading's calls end when it is cut, however slowly the source sends."""

from __future__ import annotations

import socket
import threading

import pytest
import requests

from mediaman.services.activity import cutoff
from mediaman.services.activity.cutoff import Cutoff


@pytest.fixture
def trickle(monkeypatch):
    """A server on localhost that sends a reply's head, then its body one byte a second. Yields
    its address and an event set once the head is sent."""
    server = socket.socket()
    for port in range(41760, 41770):
        try:
            server.bind(("127.0.0.1", port))
            break
        except OSError:
            continue
    else:
        pytest.skip("no free port in 41760-41769")
    server.listen(1)
    # The tests' resolver sends every name off the machine; this one name is the server.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))],
    )
    headed = threading.Event()
    done = threading.Event()

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            headed.set()
            while not done.wait(1.0):
                try:
                    conn.sendall(b"x")
                except OSError:
                    return

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://trickle.test:{port}/", headed
    done.set()
    server.close()


def test_a_call_still_trickling_ends_when_its_reading_is_cut(trickle):
    url, headed = trickle
    cut = Cutoff()
    session = cutoff.session()
    ended: list[BaseException] = []

    def call() -> None:
        try:
            cut.run(lambda: session.get(url, timeout=(1, 5)))
        except requests.RequestException as exc:
            ended.append(exc)

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    assert headed.wait(2)  # the body now comes a byte a second for 100 s
    assert caller.is_alive()
    cut.cut()
    caller.join(1.0)
    assert not caller.is_alive()
    assert ended  # with a connection error, not a reply
