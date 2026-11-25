"""A renderer that lives in memory: sockets, a clock and no network."""

from __future__ import annotations

import ipaddress
import re

from mediaman.services.dlna.wire import Wire

TV = "192.168.1.20"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
LOCATION = f"http://{TV}:7676/smp_14_"

DESCRIPTION = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
<UDN>uuid:1234-abcd</UDN>
<friendlyName>[TV] Living
 room</friendlyName><serviceList>
<service><serviceType>{RC}</serviceType><controlURL>/smp_17_</controlURL></service>
<service><serviceType>{AVT}</serviceType><controlURL>/smp_22_</controlURL></service>
</serviceList></device></root>"""


def http(body: str | bytes, status: str = "200 OK", headers: str = "") -> bytes:
    data = body.encode() if isinstance(body, str) else body
    return f"HTTP/1.1 {status}\r\nContent-Length: {len(data)}\r\n{headers}\r\n".encode() + data


def soap(action: str, inner: str = "") -> bytes:
    return http(
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<u:{action}Response xmlns:u="{AVT}">{inner}</u:{action}Response></s:Body></s:Envelope>'
    )


def fault(code: str, text: str = "x") -> bytes:
    return http(
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
        '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode><errorDescription>{text}</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>",
        "500 Internal Server Error",
    )


def ssdp(location: str, sender: str = TV) -> tuple[bytes, tuple[str, int]]:
    return f"HTTP/1.1 200 OK\r\nLocation: {location}\r\n\r\n".encode(), (sender, 1900)


class FakeTv:
    """Answers each request with what *answer* returns for its path."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.off = False
        self.datagrams = [ssdp(LOCATION)]
        self.pages: dict[str, bytes] = {"/smp_14_": http(DESCRIPTION)}
        self.actions: dict[str, bytes] = {}
        self.seconds_per_read = 0.0
        self.seconds_to_connect = 0.0
        self.slow: dict[str, float] = {}
        self.reset_after_answer = False
        self.read_size = 65536
        self.connect_timeouts: list[float] = []
        self.connects: list[tuple[str, int]] = []
        self.sent_udp: list[tuple[bytes, tuple[str, int]]] = []
        self.requests: list[bytes] = []
        self.bytes_read = 0
        self.closed = 0
        self.own = [ipaddress.IPv4Network("172.17.0.0/16")]

    def wire(self) -> Wire:
        return Wire(
            open_tcp=self._tcp,
            open_udp=lambda: _Udp(self),
            clock=lambda: self.now,
            networks=lambda: self.own,
        )

    def _tcp(self, address: str, port: int, timeout: float) -> _Tcp:
        self.connects.append((address, port))
        self.connect_timeouts.append(timeout)
        self.now += self.seconds_to_connect
        if self.off:
            self.now += timeout
            raise TimeoutError
        return _Tcp(self)

    def reply(self, request: bytes) -> bytes:
        action = re.search(rb'SOAPACTION: "[^"]*#(\w+)"', request)
        if action:
            name = action[1].decode()
            return self.actions.get(name, soap(name))
        path = request.split(b" ")[1].decode()
        return self.pages.get(path, http("", "404 Not Found"))


class _Base:
    def __init__(self, tv: FakeTv) -> None:
        self.tv = tv
        self.timeout = 0.0

    def settimeout(self, value: float | None) -> None:
        assert value
        self.timeout = value

    def close(self) -> None:
        self.tv.closed += 1

    def pass_time(self, seconds: float) -> None:
        self.tv.now += min(seconds, self.timeout)
        if seconds >= self.timeout:
            raise TimeoutError


class _Tcp(_Base):
    def __init__(self, tv: FakeTv) -> None:
        super().__init__(tv)
        self.request = b""
        self.answer: bytes | None = None

    def sendall(self, data: bytes) -> None:
        self.request += data

    def recv_into(self, buffer: memoryview) -> int:
        if self.answer is None:
            self.tv.requests.append(self.request)
            self.answer = self.tv.reply(self.request)
            for action, seconds in self.tv.slow.items():
                if f'#{action}"'.encode() in self.request:
                    self.pass_time(seconds)
        if self.tv.seconds_per_read:
            self.pass_time(self.tv.seconds_per_read)
        if self.tv.reset_after_answer and not self.answer:
            raise ConnectionResetError
        chunk = self.answer[: min(len(buffer), self.tv.read_size)]
        buffer[: len(chunk)] = chunk
        self.answer = self.answer[len(chunk) :]
        self.tv.bytes_read += len(chunk)
        return len(chunk)


class _Udp(_Base):
    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        self.tv.sent_udp.append((data, address))
        return len(data)

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        if not self.tv.datagrams:
            self.tv.now += self.timeout
            raise TimeoutError
        if self.tv.seconds_per_read:
            self.pass_time(self.tv.seconds_per_read)
        data, sender = self.tv.datagrams.pop(0)
        return data[:size], sender
