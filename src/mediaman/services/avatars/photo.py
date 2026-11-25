"""Check an uploaded photo by its bytes and rebuild it from the parts that make the picture.

Nothing here decodes pixels. The size is read from the picture's own headers, and the file
is put together again from known parts, so metadata (where and when a photo was taken), colour
profiles, thumbnails and anything hidden after the picture are left out. Only which way up
the photo is shown is written anew.
"""

from __future__ import annotations

import re
import zlib
from typing import Literal

MAX_BYTES = 10 * 1024 * 1024
MIN_SIDE = 128
#: A small file can unpack to a huge picture in everyone's browser, so its size is bounded.
MAX_SIDE = 4096

_MAX_PARTS = 10_000
_MAX_SCANS = 100
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_FRAMES = {0xC0, 0xC1, 0xC2}
_JPEG_KEPT = {0xC4, 0xDB, 0xDD}
_JPEG_FILL = re.compile(rb"\xff+")
# A marker ends a scan; FF00 (a stuffed byte) and the restart markers are part of it.
_JPEG_SCAN_END = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")
_PNG_KEPT = {b"IHDR", b"PLTE", b"tRNS", b"sRGB", b"gAMA", b"IDAT", b"IEND"}
_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_WEBP_ALPHA = 0x10
_WEBP_EXIF = 0x08
_EXIF_HEADER = b"Exif\0\0"
_ORIENTATION_TAG = 0x0112
_SHORT = 3


class PhotoError(Exception):
    """The photo cannot be used; ``reason`` is ``not_a_photo``, ``too_small`` or ``too_large``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def too_big_message(limit: int = MAX_BYTES) -> str:
    """What a person is told of a photo over *limit* bytes."""
    unit, size = ("MB", 1024 * 1024) if limit >= 1024 * 1024 else ("KB", 1024)
    return f"That photo is too big. The most is {round(limit / size, 1):g} {unit}."


def content_type(data: bytes) -> str | None:
    """The image type *data* starts as, or ``None`` for anything but JPEG, PNG and WebP."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(_PNG_SIGNATURE):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean(data: bytes) -> bytes:
    """Return *data* rebuilt without its metadata.

    Raises:
        PhotoError: it is not a whole, still JPEG, PNG or WebP of a size that can be used.
    """
    kind = content_type(data)
    if kind == "image/jpeg":
        return _clean_jpeg(data)
    if kind == "image/png":
        return _clean_png(data)
    if kind == "image/webp":
        return _clean_webp(data)
    raise _not_a_photo()


def _not_a_photo() -> PhotoError:
    return PhotoError("not_a_photo")


def _check_size(width: int, height: int) -> None:
    if not width or not height:
        raise _not_a_photo()
    if width < MIN_SIDE or height < MIN_SIDE:
        raise PhotoError("too_small")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise PhotoError("too_large")


def _orientation_only(exif: bytes) -> bytes:
    """A TIFF block that tells the orientation *exif* does and nothing else, or ``b""``."""
    tiff = exif.removeprefix(_EXIF_HEADER)
    order: Literal["little", "big"]
    if tiff[:4] == b"II*\0":
        order = "little"
    elif tiff[:4] == b"MM\0*":
        order = "big"
    else:
        return b""
    start = int.from_bytes(tiff[4:8], order)
    count = int.from_bytes(tiff[start : start + 2], order)
    for at in range(start + 2, min(start + 2 + 12 * count, len(tiff) - 11), 12):
        if int.from_bytes(tiff[at : at + 2], order) != _ORIENTATION_TAG:
            continue
        kind = int.from_bytes(tiff[at + 2 : at + 4], order)
        value = int.from_bytes(tiff[at + 8 : at + 10], order)
        if kind != _SHORT or not 2 <= value <= 8:
            return b""
        entry = _ORIENTATION_TAG.to_bytes(2) + b"\0\x03\0\0\0\x01" + value.to_bytes(2) + b"\0\0"
        return b"MM\0*\0\0\0\x08\0\x01" + entry + b"\0\0\0\0"
    return b""


def _clean_jpeg(data: bytes) -> bytes:
    out = [data[:2]]
    at = 2
    framed = False
    exif_seen = False
    scans = 0
    for _ in range(_MAX_PARTS):
        fill = _JPEG_FILL.match(data, at)
        if fill is None or fill.end() == len(data):
            raise _not_a_photo()
        # Any number of FF bytes may stand before a marker's own.
        at = fill.end() - 1
        marker = data[at + 1]
        if marker == 0xD9:
            if not scans:
                raise _not_a_photo()
            out.append(b"\xff\xd9")
            return b"".join(out)
        length = int.from_bytes(data[at + 2 : at + 4])
        end = at + 2 + length
        if length < 2 or end > len(data):
            raise _not_a_photo()
        body = data[at + 4 : end]
        if marker in _JPEG_FRAMES:
            if framed or len(body) < 6:
                raise _not_a_photo()
            framed = True
            _check_size(int.from_bytes(body[3:5]), int.from_bytes(body[1:3]))
            if body[5] not in (1, 3, 4) or len(body) != 6 + 3 * body[5]:
                raise _not_a_photo()
            out.append(data[at:end])
        elif marker == 0xDA:
            scans += 1
            if not framed or scans > _MAX_SCANS:
                raise _not_a_photo()
            stop = _JPEG_SCAN_END.search(data, end)
            if stop is None:
                raise _not_a_photo()
            end = stop.start()
            out.append(data[at:end])
        elif marker in _JPEG_KEPT or (
            # It tells how the colours are stored; without it some photos show inverted.
            marker == 0xEE and len(body) == 12 and body.startswith(b"Adobe")
        ):
            out.append(data[at:end])
        elif marker == 0xE1 and body.startswith(_EXIF_HEADER) and not exif_seen:
            exif_seen = True
            if turned := _orientation_only(body):
                segment = _EXIF_HEADER + turned
                out.append(b"\xff\xe1" + (len(segment) + 2).to_bytes(2) + segment)
        elif not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            raise _not_a_photo()
        at = end
    raise _not_a_photo()


def _clean_png(data: bytes) -> bytes:
    out = [_PNG_SIGNATURE]
    at = len(_PNG_SIGNATURE)
    has_pixels = False
    exif_seen = False
    for part in range(_MAX_PARTS):
        length = int.from_bytes(data[at : at + 4])
        name = data[at + 4 : at + 8]
        end = at + 12 + length
        if end > len(data) or (name == b"IHDR") != (part == 0):
            raise _not_a_photo()
        body = data[at + 8 : end - 4]
        if zlib.crc32(name + body) != int.from_bytes(data[end - 4 : end]):
            raise _not_a_photo()
        if name == b"IHDR":
            if length != 13 or body[8] not in _PNG_DEPTHS.get(body[9], ()):
                raise _not_a_photo()
            _check_size(int.from_bytes(body[:4]), int.from_bytes(body[4:8]))
        elif name == b"acTL":
            raise _not_a_photo()
        elif name == b"IDAT":
            has_pixels = True
        elif name == b"eXIf" and not exif_seen:
            exif_seen = True
            if turned := _orientation_only(body):
                crc = zlib.crc32(name + turned).to_bytes(4)
                out.append(len(turned).to_bytes(4) + name + turned + crc)
        if name in _PNG_KEPT:
            out.append(data[at:end])
        if name == b"IEND":
            if length or not has_pixels:
                raise _not_a_photo()
            return b"".join(out)
        at = end
    raise _not_a_photo()


def _clean_webp(data: bytes) -> bytes:
    size = int.from_bytes(data[4:8], "little") + 8
    if size > len(data):
        raise _not_a_photo()
    extended: bytes | None = None
    alpha = b""
    at = 12
    for _ in range(_MAX_PARTS):
        name = data[at : at + 4]
        length = int.from_bytes(data[at + 4 : at + 8], "little")
        end = at + 8 + length
        if at + 8 > size or end > size:
            raise _not_a_photo()
        body = data[at + 8 : end]
        if name == b"VP8X":
            if extended is not None or length != 10 or at != 12 or body[0] & 0x02:
                raise _not_a_photo()
            extended = body
        elif name == b"ALPH":
            alpha = _webp_chunk(name, body)
        elif name in (b"ANIM", b"ANMF"):
            raise _not_a_photo()
        elif name in (b"VP8 ", b"VP8L"):
            width, height = _webp_size(name, body)
            _check_size(width, height)
            # A lossless picture carries its own transparency.
            alpha = alpha if name == b"VP8 " else b""
            chunks = exif = b""
            if extended is not None:
                canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
                if extended[4:] != canvas:
                    raise _not_a_photo()
                flags = extended[0] & _WEBP_ALPHA
                if turned := _webp_orientation(data[end + length % 2 : size]):
                    flags |= _WEBP_EXIF
                    exif = _webp_chunk(b"EXIF", turned)
                chunks = _webp_chunk(b"VP8X", bytes((flags, 0, 0, 0)) + canvas) + alpha
            content = b"WEBP" + chunks + _webp_chunk(name, body) + exif
            return b"RIFF" + len(content).to_bytes(4, "little") + content
        at = end + length % 2
    raise _not_a_photo()


def _webp_chunk(name: bytes, body: bytes) -> bytes:
    return name + len(body).to_bytes(4, "little") + body + b"\0" * (len(body) % 2)


def _webp_orientation(rest: bytes) -> bytes:
    """What the first EXIF chunk after the picture tells of its orientation."""
    at = 0
    for _ in range(_MAX_PARTS):
        length = int.from_bytes(rest[at + 4 : at + 8], "little")
        end = at + 8 + length
        if at + 8 > len(rest) or end > len(rest):
            break
        if rest[at : at + 4] == b"EXIF":
            return _orientation_only(rest[at + 8 : end])
        at = end + length % 2
    return b""


def _webp_size(name: bytes, body: bytes) -> tuple[int, int]:
    if name == b"VP8 ":
        # The first bit is 0 for a key frame, the only kind a still picture can be.
        if len(body) < 10 or body[0] & 1 or body[3:6] != b"\x9d\x01\x2a":
            raise _not_a_photo()
        return (
            int.from_bytes(body[6:8], "little") & 0x3FFF,
            int.from_bytes(body[8:10], "little") & 0x3FFF,
        )
    if len(body) < 5 or body[0] != 0x2F:
        raise _not_a_photo()
    bits = int.from_bytes(body[1:5], "little")
    return (bits & 0x3FFF) + 1, (bits >> 14 & 0x3FFF) + 1
