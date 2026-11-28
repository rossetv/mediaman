"""Calls that end when a reading's time is up.

A timeout in ``requests`` limits each wait for a byte, so a source that trickles one byte a
second can hold a call for as long as it likes. Each reading hands its sources sessions from
``session()`` and runs each under one ``Cutoff``; when the reading's time is up, ``cut`` shuts
every socket they opened, and a call still waiting on one ends there with a connection error.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

# The Cutoff of the call this thread is making: connections register their socket with it.
_current = threading.local()


def _shut(sock: socket.socket) -> None:
    # socket.socket's own shutdown: an SSL socket's would drop its TLS state under the thread
    # still reading from it.
    with contextlib.suppress(OSError):
        socket.socket.shutdown(sock, socket.SHUT_RDWR)


class Cutoff:
    """The sockets of one reading's calls, all shut at once by ``cut``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._socks: list[socket.socket] = []
        self._cut = False

    def track(self, sock: socket.socket) -> None:
        with self._lock:
            if not self._cut:
                self._socks.append(sock)
                return
        _shut(sock)  # connected after the cut

    def cut(self) -> None:
        with self._lock:
            self._cut = True
            socks, self._socks = self._socks, []
        for sock in socks:
            _shut(sock)

    def run[T](self, fn: Callable[[], T]) -> T:
        """Call *fn* with the sockets it opens tracked by this Cutoff."""
        _current.cutoff = self
        try:
            return fn()
        finally:
            _current.cutoff = None


def session() -> requests.Session:
    """A session whose connections are tracked by the Cutoff the calling thread runs under."""
    tracked = requests.Session()
    adapter = _TrackedAdapter()
    tracked.mount("http://", adapter)
    tracked.mount("https://", adapter)
    return tracked


def _track(sock: socket.socket | None) -> None:
    cutoff: Cutoff | None = getattr(_current, "cutoff", None)
    if cutoff is not None and sock is not None:
        cutoff.track(sock)


class _TrackedHTTP(HTTPConnection):
    def connect(self) -> None:
        super().connect()
        _track(self.sock)


class _TrackedHTTPS(HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        _track(self.sock)


class _HTTPPool(HTTPConnectionPool):
    ConnectionCls = _TrackedHTTP


class _HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _TrackedHTTPS


class _TrackedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _HTTPPool, "https": _HTTPSPool}
