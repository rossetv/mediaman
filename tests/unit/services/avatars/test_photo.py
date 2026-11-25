"""An uploaded photo is judged by its bytes and kept without its metadata."""

from __future__ import annotations

import pytest

from mediaman.services.avatars import photo
from tests.unit.services.avatars import photos

_SECRET = b"GPS 51.5N 0.12W"


@pytest.mark.parametrize("data", [photos.jpeg(), photos.png(), photos.webp()])
def test_a_plain_photo_is_kept_as_it_is(data):
    assert photo.clean(data) == data


@pytest.mark.parametrize(
    ("data", "plain"),
    [
        (
            photos.jpeg(
                200,
                200,
                photos.segment(0xE1, b"Exif\0\0" + _SECRET),
                photos.segment(0xE2, _SECRET),
                photos.segment(0xFE, _SECRET),
                photos.segment(0xEE, _SECRET),
            )
            + _SECRET,
            photos.jpeg(),
        ),
        (
            photos.png(
                200,
                200,
                photos.png_chunk(b"eXIf", _SECRET),
                photos.png_chunk(b"tEXt", _SECRET),
                photos.png_chunk(b"iCCP", _SECRET),
            )
            + _SECRET,
            photos.png(),
        ),
        (
            photos.webp(flags=0x2C, extra=photos.webp_chunk(b"EXIF", _SECRET)) + _SECRET,
            photos.webp(flags=0),
        ),
    ],
    ids=["jpeg", "png", "webp"],
)
def test_metadata_and_whatever_follows_the_picture_are_left_out(data, plain):
    assert photo.clean(data) == plain


def test_the_marker_that_tells_how_a_jpeg_stores_its_colours_is_kept():
    data = photos.jpeg(200, 200, photos.segment(0xEE, b"Adobe" + b"\0" * 7))
    assert photo.clean(data) == data


_MAKERS = [photos.jpeg, photos.png, photos.webp, photos.webp_lossless]


@pytest.mark.parametrize("make", _MAKERS)
@pytest.mark.parametrize(
    ("size", "reason"),
    [
        ((127, 200), "too_small"),
        ((200, 127), "too_small"),
        ((4097, 200), "too_large"),
        ((200, 4097), "too_large"),
    ],
)
def test_a_photo_outside_the_bounds_is_refused(make, size, reason):
    with pytest.raises(photo.PhotoError) as refused:
        photo.clean(make(*size))
    assert refused.value.reason == reason


@pytest.mark.parametrize("make", _MAKERS)
@pytest.mark.parametrize("size", [(128, 4096), (4096, 128)])
def test_a_photo_at_the_bounds_is_taken(make, size):
    assert photo.clean(make(*size))


_FRAME = b"\x08\x00\xc8\x00\xc8\x01\x01\x11\x00"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"GIF89a" + b"\0" * 40,
        b"\xff\xd8\xff",
        b"\xff\xd8" + b"\xff" * 50,
        b"\xff\xd8\xff\xd9",
        photos.jpeg()[:-2],
        photos.jpeg(200, 200, photos.segment(0xC0, _FRAME)),
        photos.jpeg(200, 200, photos.segment(0xC3, _FRAME)),
        photos.jpeg().replace(b"\xff\xd9", photos.segment(0xDA, b"\0" * 6) * 101 + b"\xff\xd9"),
        photos.png()[:-1],
        photos.png().replace(b"IDAT", b"IDAX"),
        photos.png(200, 200, photos.png_chunk(b"acTL", b"\0" * 8)),
        photos.png(200, 200, photos.png()[8:33]),
        photos.png()[:8] + photos.png()[33:],
        photos.png()[:29] + b"\0\0\0\0" + photos.png()[33:],
        photos.png(0, 200),
        photos.webp()[:-1],
        photos.webp(flags=0x02),
        photos.webp(flags=0)[:24] + b"\xff\xff\xff" + photos.webp(flags=0)[27:],
        b"RIFF\x04\0\0\0WEBP",
        photos.webp(flags=0).replace(b"VP8 ", b"ANMF"),
    ],
)
def test_a_file_that_is_not_a_whole_still_photo_is_refused(data):
    with pytest.raises(photo.PhotoError) as refused:
        photo.clean(data)
    assert refused.value.reason == "not_a_photo"
