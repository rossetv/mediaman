"""What a TV is told about a film or a picture: DIDL-Lite, and its DLNA headers."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from xml.sax.saxutils import escape

_URL = re.compile(r"https?://[!-~]+")
_PICTURE = "image/jpeg"
_MIME = re.compile(rf"video/[A-Za-z0-9.+-]+|{_PICTURE}")
_NOT_XML = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")

# Streaming and background transfer, connection stalling, DLNA 1.5.
_FLAGS = "01700000" + "0" * 24
# Interactive and background transfer, connection stalling, DLNA 1.5.
_PICTURE_FLAGS = "00F00000" + "0" * 24


def xml_text(value: str) -> str:
    """Escape *value* for XML text or an attribute, dropping what XML cannot hold."""
    return escape(_NOT_XML.sub("", value), {'"': "&quot;", "'": "&apos;"})


def clock_time(seconds: int) -> str:
    """Return *seconds* as UPnP's ``H:MM:SS``."""
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


@dataclass(frozen=True)
class Media:
    """A stream for a TV, or a picture of at most 640 by 480.

    *byte_ranges* says its server honours ``Range``.
    """

    url: str
    title: str
    mime: str = "video/mpeg"
    byte_ranges: bool = False
    converted: bool = True
    seconds: int | None = None
    size: int | None = None
    subtitle_url: str | None = None

    def __post_init__(self) -> None:
        for url in (self.url, self.subtitle_url):
            if url is not None and not _URL.fullmatch(url):
                raise ValueError("a TV needs a plain http or https address")
        if self.seconds is not None and not (math.isfinite(self.seconds) and self.seconds >= 0):
            raise ValueError("a film needs a real length")
        if self.size is not None and (type(self.size) is not int or self.size < 0):
            raise ValueError("a film needs a real size")
        if not _MIME.fullmatch(self.mime):
            raise ValueError("a TV needs a video type or a JPEG")

    @property
    def _is_picture(self) -> bool:
        return self.mime == _PICTURE

    @property
    def content_features(self) -> str:
        """The fourth field of ``protocolInfo``, also sent as a header."""
        return (
            f"{'DLNA.ORG_PN=JPEG_SM;' if self._is_picture else ''}"
            f"DLNA.ORG_OP=0{int(self.byte_ranges)};"
            f"DLNA.ORG_CI={int(self.converted)};"
            f"DLNA.ORG_FLAGS={_PICTURE_FLAGS if self._is_picture else _FLAGS}"
        )

    def didl(self) -> str:
        """Return the DIDL-Lite document, not yet escaped for SOAP."""
        size = "" if self.size is None else f' size="{self.size}"'
        duration = "" if self.seconds is None else f' duration="{clock_time(self.seconds)}.000"'
        kind = "imageItem.photo" if self._is_picture else "videoItem"
        captions = ""
        if self.subtitle_url is not None:
            url = xml_text(self.subtitle_url)
            captions = "".join(
                f'<sec:{name} sec:type="srt">{url}</sec:{name}>'
                for name in ("CaptionInfo", "CaptionInfoEx")
            )
        return (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
            ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
            ' xmlns:sec="http://www.sec.co.kr/">'
            '<item id="0" parentID="-1" restricted="1">'
            f"<dc:title>{xml_text(self.title)}</dc:title>"
            f"<upnp:class>object.item.{kind}</upnp:class>"
            f'<res protocolInfo="http-get:*:{self.mime}:{self.content_features}"'
            f"{size}{duration}>{xml_text(self.url)}</res>{captions}</item></DIDL-Lite>"
        )

    def stream_headers(self, request: Mapping[str, str]) -> dict[str, str]:
        """Return the headers the stream owes a TV that sent *request*'s headers."""
        asked = {name.lower(): value.strip() for name, value in request.items()}
        headers = {"transferMode.dlna.org": "Interactive" if self._is_picture else "Streaming"}
        if asked.get("getcontentfeatures.dlna.org") == "1":
            headers["contentFeatures.dlna.org"] = self.content_features
        if asked.get("getcaptioninfo.sec") == "1" and self.subtitle_url is not None:
            headers["CaptionInfo.sec"] = self.subtitle_url
        return headers
