"""Small photos built by hand, whole enough for every check that reads headers."""

from __future__ import annotations

import struct
import zlib


def png_chunk(name: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body))


def png(width: int = 200, height: int = 200, *extra: bytes) -> bytes:
    rows = zlib.compress(b"\0" * ((width + 1) * height)) if width * height < 10**6 else b"x"
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)),
            *extra,
            png_chunk(b"IDAT", rows),
            png_chunk(b"IEND", b""),
        )
    )


def segment(marker: int, body: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(body) + 2) + body


def jpeg(width: int = 200, height: int = 200, *extra: bytes) -> bytes:
    frame = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    return b"".join(
        (
            b"\xff\xd8",
            *extra,
            segment(0xDB, b"\0" * 65),
            segment(0xC0, frame),
            segment(0xC4, b"\0" * 17),
            segment(0xDA, b"\x01\x01\x00\x00\x3f\x00"),
            b"pixels\xff\x00\xff\xd0more",
            b"\xff\xd9",
        )
    )


def webp_chunk(name: bytes, body: bytes) -> bytes:
    return name + struct.pack("<I", len(body)) + body + b"\0" * (len(body) % 2)


def webp(
    width: int = 200, height: int = 200, *, flags: int | None = None, extra: bytes = b""
) -> bytes:
    """Lossy; extended with *flags*, and *extra* chunks after the picture."""
    head = b""
    if flags is not None:
        canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
        head = webp_chunk(b"VP8X", bytes((flags, 0, 0, 0)) + canvas)
    frame = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"pixels"
    body = b"WEBP" + head + webp_chunk(b"VP8 ", frame) + extra
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_lossless(width: int, height: int) -> bytes:
    bits = (width - 1) | (height - 1) << 14
    body = b"WEBP" + webp_chunk(b"VP8L", b"\x2f" + struct.pack("<I", bits) + b"pixel")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def exif(orientation: int, secret: bytes, order: str = "<") -> bytes:
    """A TIFF block telling *orientation*, between a maker's name and a place holding *secret*."""
    entries = (
        struct.pack(order + "HHII", 0x010F, 2, len(secret), 50),
        struct.pack(order + "HHIHH", 0x0112, 3, 1, orientation, 0),
        struct.pack(order + "HHII", 0x8825, 4, 1, 50),
    )
    start = b"II*\0" if order == "<" else b"MM\0*"
    return (
        start
        + struct.pack(order + "IH", 8, len(entries))
        + b"".join(entries)
        + struct.pack(order + "I", 0)
        + secret
    )


def orientation_only(orientation: int) -> bytes:
    entry = struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0)
    return b"MM\0*" + struct.pack(">IH", 8, 1) + entry + struct.pack(">I", 0)
