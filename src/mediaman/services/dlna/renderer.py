"""A TV as a UPnP MediaRenderer: found, described and controlled, always by polling."""

from __future__ import annotations

import contextlib
import math
import re
from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as ET

from mediaman.services.dlna.addresses import check_device_url
from mediaman.services.dlna.errors import TvBadAnswer, TvError, TvFault, TvUnreachable
from mediaman.services.dlna.media import Media, clock_time, xml_text
from mediaman.services.dlna.wire import Wire

_MAX_NAME = 80
_MAX_UDN = 200
_ASK_SECONDS = 3.0
# A TV may fetch the start of the film before it answers.
_COMMAND_SECONDS = 20.0
_FIND_SECONDS = 10.0
_GUESS_SECONDS = 1.0
# Where TVs keep their description, for those that do not answer a search.
_GUESSES = (
    (9197, "/dmr"),
    (7676, "/smp_14_"),
    (7676, "/smp_15_"),
    (7676, "/smp_7_"),
    (52235, "/dmr/SamsungMRDesc.xml"),
    (52323, "/dmr.xml"),
    (55000, "/dmr/ddd.xml"),
    (49152, "/description.xml"),
)
_PADDING = b"\0 \t\r\n"
_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:"
_SERVICE = re.compile(r"urn:schemas-upnp-org:service:(AVTransport|RenderingControl):[1-9]")
_TIME = re.compile(r"([0-9]{1,3}):([0-5][0-9]):([0-5][0-9])(?:\.[0-9/]+)?")
_STATES = frozenset({"STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING", "NO_MEDIA_PRESENT"})

_CANNOT_READ = "The TV gave an answer that cannot be read."
_NOT_NOW = "The TV cannot do that right now."
_NOT_PLAYABLE = "The TV cannot play this kind of video."
_CANNOT_SEEK = "The TV cannot jump to that point."
_FAULTS = {
    401: "The TV does not know how to do that.",
    402: "The TV did not accept the request.",
    701: _NOT_NOW,
    702: "The TV found nothing to play.",
    703: "The TV could not read the video.",
    704: _NOT_PLAYABLE,
    705: "The TV is locked by another device.",
    710: _CANNOT_SEEK,
    711: _CANNOT_SEEK,
    714: _NOT_PLAYABLE,
    715: "The video is already in use.",
    716: "The TV could not reach the video.",
    717: _NOT_NOW,
    718: _NOT_NOW,
}


@dataclass(frozen=True)
class Status:
    """What the TV is doing. Times are seconds, None when the TV does not say."""

    state: str
    trouble: bool
    position: int | None
    duration: int | None


@dataclass(frozen=True)
class _Service:
    type: str
    port: int
    path: str
    loudest: int = 100


@dataclass(frozen=True)
class Renderer:
    """A TV that can be told what to play."""

    address: str
    location: str
    name: str
    udn: str
    _wire: Wire = field(repr=False)
    _transport: _Service = field(repr=False)
    _rendering: _Service | None = field(repr=False)

    def load(self, media: Media) -> None:
        """Hand the TV what to play; :meth:`play` starts it."""
        with self._stopped_if_lost():
            self._command(
                "SetAVTransportURI", CurrentURI=media.url, CurrentURIMetaData=media.didl()
            )

    def play(self) -> None:
        with self._stopped_if_lost():
            self._command("Play", Speed="1")

    def pause(self) -> None:
        self._command("Pause")

    def stop(self) -> None:
        self._command("Stop")

    def seek(self, seconds: int) -> None:
        if not math.isfinite(seconds):
            raise ValueError("a jump needs a finite time")
        self._command("Seek", Unit="REL_TIME", Target=clock_time(max(0, seconds)))

    def status(self) -> Status:
        """Ask the TV what it is doing; raises ``TvUnreachable`` once it is gone."""
        transport = self._av("GetTransportInfo")
        position = self._av("GetPositionInfo")
        state = transport.get("CurrentTransportState", "")
        return Status(
            state=state if state in _STATES else "UNKNOWN",
            trouble=transport.get("CurrentTransportStatus") == "ERROR_OCCURRED",
            position=_seconds(position.get("RelTime", "")),
            duration=_seconds(position.get("TrackDuration", "")) or None,
        )

    @property
    def has_volume(self) -> bool:
        return self._rendering is not None

    def volume(self) -> int:
        """Return the volume, 0 to 100."""
        service = self._volume_service()
        answer = self._ask(service, "GetVolume", {"Channel": "Master"})
        value = answer.get("CurrentVolume", "")
        if not re.fullmatch(r"[0-9]{1,5}", value):
            raise TvBadAnswer(_CANNOT_READ)
        return min(100, round(int(value) * 100 / service.loudest))

    def set_volume(self, volume: int) -> None:
        service = self._volume_service()
        wanted = round(min(100, max(0, volume)) * service.loudest / 100)
        self._call(service, "SetVolume", {"Channel": "Master", "DesiredVolume": str(wanted)})

    @contextlib.contextmanager
    def _stopped_if_lost(self) -> Iterator[None]:
        """Stop a TV that may have started although its answer never came."""
        try:
            yield
        except TvUnreachable:
            with contextlib.suppress(TvError):
                self._call(self._transport, "Stop", {})
            raise

    def _av(self, action: str) -> dict[str, str]:
        return self._ask(self._transport, action, {})

    def _command(self, action: str, **arguments: str) -> None:
        self._call(self._transport, action, arguments, _COMMAND_SECONDS)

    def _volume_service(self) -> _Service:
        if self._rendering is None:
            raise TvFault(_FAULTS[401], 401)
        return self._rendering

    def _ask(self, service: _Service, action: str, arguments: dict[str, str]) -> dict[str, str]:
        root = _parse(self._call(service, action, arguments))
        response = next((e for e in root.iter() if _local(e) == f"{action}Response"), None)
        if response is None:
            raise TvBadAnswer(_CANNOT_READ)
        return {_local(e): _text(e) for e in response}

    def _call(
        self,
        service: _Service,
        action: str,
        arguments: dict[str, str],
        seconds: float = _ASK_SECONDS,
    ) -> bytes:
        fields = "".join(
            f"<{name}>{xml_text(value)}</{name}>"
            for name, value in {"InstanceID": "0", **arguments}.items()
        )
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service.type}">{fields}</u:{action}>'
            "</s:Body></s:Envelope>"
        )
        status, answer = self._wire.request(
            self.address,
            service.port,
            service.path,
            body.encode("utf-8"),
            {
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{service.type}#{action}"',
            },
            seconds,
        )
        if status in (401, 403):
            raise TvFault("The TV did not allow mediaman. Allow it on the TV, then try again.", 0)
        if status not in (200, 500):
            raise TvBadAnswer(_CANNOT_READ)
        try:
            root = _parse(answer)
        except TvBadAnswer:
            if status == 200:
                return answer
            raise
        if status == 500 or any(_local(e) == "Fault" for e in root.iter()):
            code = next((_text(e) for e in root.iter() if _local(e) == "errorCode"), "")
            number = int(code) if re.fullmatch(r"[0-9]{3}", code) else 0
            raise TvFault(_FAULTS.get(number, "The TV could not do that."), number)
        return answer


def find(address: str, wire: Wire) -> Renderer:
    """Look for a TV at *address*, as an admin typed it."""
    address = wire.check(address)
    remaining = wire.deadline(_FIND_SECONDS)
    try:
        location = wire.search(address, lambda url: check_device_url(url, address))
    except TvUnreachable:
        guessed = _guess(address, wire, remaining)
        if guessed is None:
            raise
        return guessed
    return describe(address, location, wire)


def _guess(address: str, wire: Wire, remaining: Callable[[], float]) -> Renderer | None:
    silent: set[int] = set()
    for port, path in _GUESSES:
        if port in silent:
            continue
        try:
            seconds = min(_GUESS_SECONDS, remaining())
        except TimeoutError:
            break
        try:
            return describe(address, f"http://{address}:{port}{path}", wire, seconds)
        except TvUnreachable:
            silent.add(port)
        except TvError:
            continue
    return None


def describe(address: str, location: str, wire: Wire, seconds: float = _ASK_SECONDS) -> Renderer:
    """Read the TV's description at *location*, as found before."""
    address = wire.check(address)
    port, path = check_device_url(location, address)
    remaining = wire.deadline(seconds)
    status, answer = wire.request(address, port, path, seconds=seconds)
    if status != 200:
        raise TvBadAnswer(_CANNOT_READ)
    root = _parse(answer)
    base = _child(root, "URLBase") or location
    check_device_url(base, address)
    for device in root.iter():
        if _local(device) == "device" and _child(device, "deviceType").startswith(_RENDERER):
            services = _services(device, address, base, wire, remaining)
            if "AVTransport" in services:
                return Renderer(
                    address=address,
                    location=location,
                    name=_clean(_child(device, "friendlyName"))[:_MAX_NAME] or address,
                    udn=_clean(_child(device, "UDN"))[:_MAX_UDN],
                    _wire=wire,
                    _transport=services["AVTransport"],
                    _rendering=services.get("RenderingControl"),
                )
    raise TvBadAnswer("That device cannot be told what to play.")


def _services(
    device: Element, address: str, base: str, wire: Wire, remaining: Callable[[], float]
) -> dict[str, _Service]:
    found: dict[str, _Service] = {}
    for services in device:
        if _local(services) != "serviceList":
            continue
        for service in services:
            match = _SERVICE.fullmatch(_child(service, "serviceType"))
            if match is None or match[1] in found:
                continue
            try:
                port, path = check_device_url(_child(service, "controlURL"), address, base)
            except TvError:
                if match[1] == "AVTransport":
                    raise
                continue
            found[match[1]] = _Service(match[0], port, path)
            if match[1] == "RenderingControl":
                url = _child(service, "SCPDURL")
                loudest = _loudest(url, address, base, wire, remaining)
                found[match[1]] = _Service(match[0], port, path, loudest)
    return found


def _loudest(url: str, address: str, base: str, wire: Wire, remaining: Callable[[], float]) -> int:
    """Return the number the TV counts its volume to, 100 when it does not say."""
    try:
        port, path = check_device_url(url, address, base)
        _, answer = wire.request(address, port, path, seconds=remaining())
        root = _parse(answer)
    except (TvError, TimeoutError):
        return 100
    for variable in root.iter():
        if _local(variable) == "stateVariable" and _child(variable, "name") == "Volume":
            maximum = next((_text(e) for e in variable.iter() if _local(e) == "maximum"), "")
            if re.fullmatch(r"[1-9][0-9]{0,4}", maximum):
                return int(maximum)
    return 100


def _clean(value: str) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())


def _parse(body: bytes) -> Element:
    # UTF-16 needs the zero bytes at its ends.
    if not body.startswith((BOM_UTF16_LE, BOM_UTF16_BE)):
        body = body.strip(_PADDING).removeprefix(BOM_UTF8).strip(_PADDING)
    try:
        root: Element = ET.fromstring(body, forbid_dtd=True)
    # defusedxml's own refusals are ValueErrors.
    except (ParseError, ValueError, LookupError) as exc:
        raise TvBadAnswer(_CANNOT_READ) from exc
    return root


def _local(element: Element) -> str:
    return element.tag.rpartition("}")[2]


def _text(element: Element) -> str:
    return (element.text or "").strip()


def _child(element: Element, name: str) -> str:
    return next((_text(e) for e in element if _local(e) == name), "")


def _seconds(value: str) -> int | None:
    match = _TIME.fullmatch(value)
    if match is None:
        return None
    return int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3])
