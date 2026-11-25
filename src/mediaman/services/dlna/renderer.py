"""A TV as a UPnP MediaRenderer: found, described and controlled, always by polling."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as ET

from mediaman.services.dlna.addresses import check_device_url
from mediaman.services.dlna.errors import TvBadAnswer, TvFault
from mediaman.services.dlna.media import Media, clock_time, xml_text
from mediaman.services.dlna.wire import Wire

_MAX_NAME = 80
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


@dataclass(frozen=True)
class Renderer:
    """A TV that can be told what to play."""

    address: str
    location: str
    name: str
    _wire: Wire = field(repr=False)
    _transport: _Service = field(repr=False)
    _rendering: _Service | None = field(repr=False)

    def load(self, media: Media) -> None:
        """Hand the TV what to play; :meth:`play` starts it."""
        self._av("SetAVTransportURI", CurrentURI=media.url, CurrentURIMetaData=media.didl())

    def play(self) -> None:
        self._av("Play", Speed="1")

    def pause(self) -> None:
        self._av("Pause")

    def stop(self) -> None:
        self._av("Stop")

    def seek(self, seconds: int) -> None:
        self._av("Seek", Unit="REL_TIME", Target=clock_time(max(0, seconds)))

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
        value = self._volume("GetVolume").get("CurrentVolume", "")
        if not re.fullmatch(r"[0-9]{1,5}", value):
            raise TvBadAnswer(_CANNOT_READ)
        return min(100, int(value))

    def set_volume(self, volume: int) -> None:
        self._volume("SetVolume", DesiredVolume=str(min(100, max(0, volume))))

    def _av(self, action: str, **arguments: str) -> dict[str, str]:
        return self._call(self._transport, action, arguments)

    def _volume(self, action: str, **arguments: str) -> dict[str, str]:
        if self._rendering is None:
            raise TvFault(_FAULTS[401], 401)
        return self._call(self._rendering, action, {"Channel": "Master", **arguments})

    def _call(self, service: _Service, action: str, arguments: dict[str, str]) -> dict[str, str]:
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
        )
        if status in (401, 403):
            raise TvFault("The TV did not allow mediaman. Allow it on the TV, then try again.", 0)
        if status not in (200, 500):
            raise TvBadAnswer(_CANNOT_READ)
        root = _parse(answer)
        if status == 500:
            code = next((_text(e) for e in root.iter() if _local(e) == "errorCode"), "")
            number = int(code) if re.fullmatch(r"[0-9]{3}", code) else 0
            raise TvFault(_FAULTS.get(number, "The TV could not do that."), number)
        response = next((e for e in root.iter() if _local(e) == f"{action}Response"), None)
        if response is None:
            raise TvBadAnswer(_CANNOT_READ)
        return {_local(e): _text(e) for e in response}


def find(address: str, wire: Wire) -> Renderer:
    """Look for a TV at *address*, as an admin typed it."""
    address = wire.check(address)
    location = wire.search(address, lambda url: check_device_url(url, address))
    return describe(address, location, wire)


def describe(address: str, location: str, wire: Wire) -> Renderer:
    """Read the TV's description at *location*, as found before."""
    address = wire.check(address)
    port, path = check_device_url(location, address)
    status, answer = wire.request(address, port, path)
    if status != 200:
        raise TvBadAnswer(_CANNOT_READ)
    root = _parse(answer)
    base = _child(root, "URLBase") or location
    check_device_url(base, address)
    for device in root.iter():
        if _local(device) == "device" and _child(device, "deviceType").startswith(_RENDERER):
            services = _services(device, address, base)
            if "AVTransport" in services:
                name = " ".join(
                    "".join(
                        c if c.isprintable() else " " for c in _child(device, "friendlyName")
                    ).split()
                )
                return Renderer(
                    address=address,
                    location=location,
                    name=name[:_MAX_NAME] or address,
                    _wire=wire,
                    _transport=services["AVTransport"],
                    _rendering=services.get("RenderingControl"),
                )
    raise TvBadAnswer("That device cannot be told what to play.")


def _services(device: Element, address: str, base: str) -> dict[str, _Service]:
    found: dict[str, _Service] = {}
    for services in device:
        if _local(services) != "serviceList":
            continue
        for service in services:
            match = _SERVICE.fullmatch(_child(service, "serviceType"))
            if match:
                port, path = check_device_url(_child(service, "controlURL"), address, base)
                found.setdefault(match[1], _Service(match[0], port, path))
    return found


def _parse(body: bytes) -> Element:
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
