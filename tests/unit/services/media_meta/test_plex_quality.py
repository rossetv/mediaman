"""Which quality choices a title gets, and which one a request is given."""

from __future__ import annotations

import pytest

from mediaman.services.media_meta.plex_quality import (
    QUALITY_PRESETS,
    may_copy,
    quality_rows,
    resolve_quality,
)
from mediaman.services.media_meta.plex_watch import PlexItem, PlexMedia

BASIC = ["best", "medium", "low"]
TOP = ["1080p-20000", "1080p-12000", "1080p-10000", "1080p-8000"]
MID = ["720p-4000", "720p-3000", "720p-2000"]


def _item(media: PlexMedia | None, *, subtitle: bool = False) -> PlexItem:
    return PlexItem("movie", "7", "Film", "", 2020, 6000, "1", None, subtitle, media=media)


def _media(width=1920, height=1080, kbps=11000, plain=True) -> PlexMedia:
    return PlexMedia(width, height, "", kbps, plain)


def _ids(media: PlexMedia | None, *, original: bool = False) -> list[str]:
    return [r["id"] for r in quality_rows(_item(media), original=original)]


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        (_media(), ["1080p-10000", "1080p-8000", *MID, "480p-1500"]),
        (_media(3840, 2160, 60000, False), [*TOP, *MID, "480p-1500"]),
        (_media(1920, 800), ["1080p-10000", "1080p-8000", *MID, "480p-1500"]),
        (_media(1440, 1080, 9000), ["1080p-8000", *MID, "480p-1500"]),
        (_media(kbps=10000), ["1080p-8000", *MID, "480p-1500"]),
        (_media(1280, 536, 3500), ["720p-3000", "720p-2000", "480p-1500"]),
        (_media(720, 576, 3000), ["480p-1500"]),
        (PlexMedia(resolution="4k", bitrate_kbps=60000), [*TOP, *MID, "480p-1500"]),
    ],
    ids=[
        "1080p",
        "4K",
        "cropped 1080p",
        "4:3 1080p",
        "exactly a step",
        "cropped 720p",
        "576p",
        "class only",
    ],
)
def test_conversions_stay_at_or_below_the_source(media, expected):
    assert _ids(media) == ["auto", *expected]


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        (_media(1920, 1080, 3500, False), ["1080p-8000", "720p-3000", "720p-2000", "480p-1500"]),
        (_media(1280, 720, 1800, False), ["720p-2000", "480p-1500"]),
        (_media(640, 360, 700), ["480p-1500"]),
    ],
)
def test_a_low_bitrate_source_keeps_its_own_resolution(media, expected):
    assert _ids(media) == ["auto", *expected]


@pytest.mark.parametrize(
    "media",
    [None, PlexMedia(), _media(kbps=None), _media(kbps=0), PlexMedia(bitrate_kbps=9000)],
    ids=["no media", "empty", "no bitrate", "zero bitrate", "no size"],
)
def test_without_media_details_the_basic_three_are_offered(media):
    assert _ids(media) == BASIC
    assert [r["detail"] for r in quality_rows(_item(media))] == ["1080p", "720p", "480p"]


def test_original_comes_after_automatic_once_plex_says_it_copies_the_video():
    rows = quality_rows(_item(_media(1920, 800, 10840)), original=True)
    assert [r["kind"] for r in rows[:3]] == ["auto", "original", "convert"]
    assert (rows[1]["label"], rows[1]["detail"]) == ("Original (1080p)", "10.8 Mbps")


@pytest.mark.parametrize(
    ("media", "subtitle"),
    [
        (None, False),
        (_media(plain=False), False),
        (_media(3840, 2160), False),
        (_media(kbps=20001), False),
        (_media(kbps=None), False),
        (_media(), True),
    ],
    ids=["no media", "not plain h264", "4K", "too high a bitrate", "no bitrate", "subtitle chosen"],
)
def test_original_is_not_offered_for(media, subtitle):
    item = _item(media, subtitle=subtitle)
    assert may_copy(item) is False
    assert "original" not in [r["id"] for r in quality_rows(item, original=True)]


@pytest.mark.parametrize(
    ("media", "asked", "given"),
    [
        (_media(), "1080p-10000", "1080p-10000"),
        (_media(), "1080p-20000", "1080p-10000"),
        (_media(1280, 720, 3500), "1080p-8000", "720p-3000"),
        (_media(1920, 1080, 3500), "720p-4000", "720p-3000"),
        (_media(640, 360, 700), "720p-2000", "480p-1500"),
        (_media(), "original", "1080p-10000"),
        (_media(3840, 2160, 60000, False), "auto", "1080p-8000"),
        (_media(1280, 720, 3500), "auto", "720p-3000"),
        (_media(1280, 720, 3500), "best", "best"),
        (None, "auto", "best"),
        (None, "original", "best"),
        (None, "1080p-20000", "best"),
        (None, "720p-3000", "low"),
        (None, "480p-1500", "low"),
    ],
)
def test_a_choice_the_title_lacks_becomes_the_nearest_lower_one(media, asked, given):
    rows = quality_rows(_item(media))
    assert resolve_quality(rows, asked) == given
    assert given in QUALITY_PRESETS


def test_original_is_given_when_it_is_on_offer():
    rows = quality_rows(_item(_media()), original=True)
    assert resolve_quality(rows, "original") == "original"
