"""The only place that talks to a TV: bounded HTTP, and unicast discovery."""

from __future__ import annotations

import contextlib
import http.client
import io
import ipaddress
import socket
import time
from collections.abc import Callable
from typing import Any, Protocol, cast

from mediaman.services.dlna.addresses import ALLOWED_PORTS, check_address, own_networks
from mediaman.services.dlna.errors import TvBadAnswer, TvRefused, TvUnreachable

MAX_BODY = 64 * 1024
_MAX_HEADERS = 16 * 1024
_MAX_SECONDS = 3.0
_MAX_DATAGRAMS = 16
_SSDP_PORT = 1900

_GONE = "The TV is not answering. It may be off or on another network."
_TOO_BIG = "The TV's answer was too big."


class Socket(Protocol):
    """The part of a socket that is used, so that a test can stand in for one."""

    def settimeout(self, value: float | None, /) -> None: ...
    def sendall(self, data: Any, /) -> None: ...
    def recv_into(self, buffer: Any, /) -> int: ...
    def sendto(self, data: bytes, address: tuple[str, int], /) -> int: ...
    def recvfrom(self, size: int, /) -> tuple[bytes, Any]: ...
    def close(self) -> None: ...


def _open_tcp(address: str, port: int, timeout: float) -> Socket:
    # Not create_connection, which would look names up.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((address, port))
    except BaseException:
        sock.close()
        raise
    return sock


def _open_udp() -> Socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


class _Bounded:
    """A socket seen by ``http.client``, with one deadline and one byte budget."""

    def __init__(self, sock: Socket, remaining: Callable[[], float]) -> None:
        self._sock = sock
        self._remaining = remaining
        self._budget = MAX_BODY + _MAX_HEADERS

    def sendall(self, data: bytes) -> None:
        self._sock.settimeout(self._remaining())
        self._sock.sendall(data)

    def makefile(self, _mode: str) -> io.BufferedReader:
        return io.BufferedReader(_Reader(self.recv_into))

    def close(self) -> None:
        """Do nothing: ``http.client`` closes before the body is read; ``Wire`` closes after."""

    def recv_into(self, buffer: Any) -> int:
        self._sock.settimeout(self._remaining())
        count = self._sock.recv_into(buffer)
        self._budget -= count
        if self._budget < 0:
            raise TvBadAnswer(_TOO_BIG)
        return count


class _Reader(io.RawIOBase):
    def __init__(self, recv_into: Callable[[Any], int]) -> None:
        self._recv_into = recv_into

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        return self._recv_into(buffer)


class Wire:
    """Sends requests to a TV, refusing every address and port not allowed."""

    def __init__(
        self,
        *,
        open_tcp: Callable[[str, int, float], Socket] = _open_tcp,
        open_udp: Callable[[], Socket] = _open_udp,
        clock: Callable[[], float] = time.monotonic,
        networks: Callable[[], list[ipaddress.IPv4Network]] = own_networks,
    ) -> None:
        self._open_tcp = open_tcp
        self._open_udp = open_udp
        self._clock = clock
        self._networks = networks

    def check(self, address: str) -> str:
        """Return *address* if a TV may be reached there."""
        return check_address(address, self._networks())

    def _deadline(self) -> Callable[[], float]:
        end = self._clock() + _MAX_SECONDS

        def remaining() -> float:
            left = end - self._clock()
            if left <= 0:
                raise TimeoutError
            return left

        return remaining

    def request(
        self,
        address: str,
        port: int,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        """GET, or POST when there is a *body*. Never follows a redirect."""
        address = self.check(address)
        if port not in ALLOWED_PORTS:
            raise TvRefused(f"mediaman does not allow port {port} on a TV.")
        remaining = self._deadline()
        sock: Socket | None = None
        try:
            sock = self._open_tcp(address, port, remaining())
            connection = http.client.HTTPConnection(address, port)
            connection.sock = cast(socket.socket, _Bounded(sock, remaining))
            connection.request(
                "GET" if body is None else "POST",
                path,
                body,
                {"Connection": "close", **(headers or {})},
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise TvRefused("The TV tried to send mediaman to another address.")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise TvBadAnswer("The TV gave an answer that cannot be read.")
            data = response.read(MAX_BODY + 1)
        except OSError as exc:
            raise TvUnreachable(_GONE) from exc
        except (http.client.HTTPException, ValueError) as exc:
            raise TvBadAnswer("The TV gave an answer that cannot be read.") from exc
        finally:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        if len(data) > MAX_BODY:
            raise TvBadAnswer(_TOO_BIG)
        if response.length:
            raise TvUnreachable(_GONE)
        return response.status, data

    def search(self, address: str, usable: Callable[[str], object]) -> str:
        """Ask *address* where its description is, and return the first *usable* answer.

        *usable* raises for a URL that is not allowed.
        """
        address = self.check(address)
        remaining = self._deadline()
        message = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {address}:{_SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 1\r\n"
            "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
        ).encode("ascii")
        refusal: TvRefused | TvBadAnswer | None = None
        sock: Socket | None = None
        try:
            sock = self._open_udp()
            sock.settimeout(remaining())
            sock.sendto(message, (address, _SSDP_PORT))
            for _ in range(_MAX_DATAGRAMS):
                sock.settimeout(remaining())
                data, sender = sock.recvfrom(2048)
                if sender[0] != address:
                    continue
                location = _location(data)
                if location is None:
                    continue
                try:
                    usable(location)
                except (TvRefused, TvBadAnswer) as exc:
                    refusal = exc
                    continue
                return location
        except OSError as exc:
            if refusal is None:
                raise TvUnreachable(_GONE) from exc
        finally:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        raise refusal or TvUnreachable(_GONE)


def _location(datagram: bytes) -> str | None:
    lines = datagram.decode("latin-1").split("\r\n")
    if lines[0].split(" ")[:2] != ["HTTP/1.1", "200"]:
        return None
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "location":
            return value.strip()
    return None
