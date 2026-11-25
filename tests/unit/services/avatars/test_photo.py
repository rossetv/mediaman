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


def _jpeg_with(exif: bytes) -> bytes:
    return photos.jpeg(200, 200, photos.segment(0xE1, b"Exif\0\0" + exif))


def _png_with(exif: bytes) -> bytes:
    return photos.png(200, 200, photos.png_chunk(b"eXIf", exif))


def _webp_with(exif: bytes) -> bytes:
    return photos.webp(flags=0x08, extra=photos.webp_chunk(b"EXIF", exif))


_WITH_EXIF = [
    (_jpeg_with, photos.jpeg()),
    (_png_with, photos.png()),
    (_webp_with, photos.webp(flags=0)),
]


@pytest.mark.parametrize("order", ["<", ">"])
@pytest.mark.parametrize("orientation", range(2, 9))
@pytest.mark.parametrize(("make", "plain"), _WITH_EXIF)
def test_the_way_up_is_all_that_is_kept_of_a_photos_exif(make, plain, orientation, order):
    cleaned = photo.clean(make(photos.exif(orientation, _SECRET, order)))

    assert cleaned == make(photos.orientation_only(orientation))
    assert _SECRET not in cleaned


@pytest.mark.parametrize(
    "exif",
    [
        photos.exif(1, _SECRET),
        photos.exif(0, _SECRET),
        photos.exif(9, _SECRET),
        photos.exif(6, _SECRET)[:20],
        b"II*\0\xff\xff\xff\xff",
    ],
)
@pytest.mark.parametrize(("make", "plain"), _WITH_EXIF)
def test_exif_that_turns_nothing_is_left_out_whole(make, plain, exif):
    assert photo.clean(make(exif)) == plain


def test_only_the_first_exif_counts():
    first, second = (photos.segment(0xE1, b"Exif\0\0" + photos.exif(n, _SECRET)) for n in (6, 3))

    cleaned = photo.clean(photos.jpeg(200, 200, first, second))

    assert cleaned == _jpeg_with(photos.orientation_only(6))


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
