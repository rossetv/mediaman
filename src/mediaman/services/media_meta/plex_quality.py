"""The player's quality choices for one Plex item, and which of them a request gets.

Rows are plain JSON-ready dicts, best first: Automatic, Original (only once Plex has said it
would copy the video), then the conversions the source is good enough for. An item Plex gives
no media details for gets the three basic choices.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from mediaman.services.media_meta.plex_watch import PlexItem, PlexMedia

QualityId = Literal[
    "auto",
    "original",
    "1080p-20000",
    "1080p-12000",
    "1080p-10000",
    "1080p-8000",
    "720p-4000",
    "720p-3000",
    "720p-2000",
    "480p-1500",
    "best",
    "medium",
    "low",
]

#: The source's own video, repackaged and not converted.
ORIGINAL = "original"

_BOXES = {1080: "1920x1080", 720: "1280x720", 480: "854x480"}

#: ``(height, kbps)`` of each conversion, best first.
_STEPS: dict[str, tuple[int, int]] = {
    "1080p-20000": (1080, 20000),
    "1080p-12000": (1080, 12000),
    "1080p-10000": (1080, 10000),
    "1080p-8000": (1080, 8000),
    "720p-4000": (720, 4000),
    "720p-3000": (720, 3000),
    "720p-2000": (720, 2000),
    "480p-1500": (480, 1500),
}

_BASIC: dict[str, tuple[int, int]] = {
    "best": (1080, 8000),
    "medium": (720, 4000),
    "low": (480, 1500),
}

#: ``(videoResolution, maxVideoBitrate kbps)``; Plex keeps the aspect ratio within the box.
QUALITY_PRESETS: dict[str, tuple[str, int]] = {
    name: (_BOXES[height], kbps) for name, (height, kbps) in {**_STEPS, **_BASIC}.items()
}

_CLASSES = (2160, 1440, 1080, 720, 480)
# A crop or an encoder's rounding leaves a picture a little short of its class.
_CLASS_TOLERANCE = 0.9
_RESOLUTION_CLASSES = {"4k": 2160, "1440": 1440, "1080": 1080, "720": 720, "480": 480, "sd": 480}

_AUTO_MAX_KBPS = 8000
# Segments of a copied video are as long as its keyframe spacing and are held whole in memory,
# so a high bitrate would run past the segment size cap.
_ORIGINAL_MAX_KBPS = 20000


class QualityRow(TypedDict):
    id: str
    kind: Literal["auto", "original", "convert"]
    label: str
    detail: str
    height: int | None
    bitrate_kbps: int | None


def _height_class(media: PlexMedia) -> int | None:
    if media.width and media.height:
        # A wide picture is cropped in height only, so its width says what it is.
        height = max(media.height, media.width * 9 / 16)
        return next((c for c in _CLASSES if height >= c * _CLASS_TOLERANCE), 480)
    return _RESOLUTION_CLASSES.get(media.resolution)


def _mbps(kbps: int) -> str:
    return f"{round(kbps / 1000, 1):g} Mbps"


def _convert_row(name: str) -> QualityRow:
    height, kbps = _STEPS[name]
    return {
        "id": name,
        "kind": "convert",
        "label": f"{height}p",
        "detail": _mbps(kbps),
        "height": height,
        "bitrate_kbps": kbps,
    }


def _basic_rows() -> list[QualityRow]:
    return [
        {
            "id": name,
            "kind": "convert",
            "label": name.capitalize(),
            "detail": f"{height}p",
            "height": height,
            "bitrate_kbps": kbps,
        }
        for name, (height, kbps) in _BASIC.items()
    ]


def may_copy(item: PlexItem) -> bool:
    """True when the item's video could be sent as it is; Plex's own decision has the last word."""
    media = item.media
    if media is None or not media.plain_h264 or item.subtitle_selected or not media.bitrate_kbps:
        return False
    height = _height_class(media)
    return height is not None and height <= 1080 and media.bitrate_kbps <= _ORIGINAL_MAX_KBPS


def quality_rows(item: PlexItem, *, original: bool = False) -> list[QualityRow]:
    """The choices for *item*; *original* is whether Plex said it would copy the video."""
    media = item.media
    height = _height_class(media) if media is not None else None
    if media is None or height is None or not media.bitrate_kbps:
        return _basic_rows()
    rows: list[QualityRow] = [
        {
            "id": "auto",
            "kind": "auto",
            "label": "Automatic",
            "detail": "",
            "height": None,
            "bitrate_kbps": None,
        }
    ]
    if original and may_copy(item):
        rows.append(
            {
                "id": ORIGINAL,
                "kind": "original",
                "label": f"Original ({f'{height}p' if height >= 720 else 'SD'})",
                "detail": _mbps(media.bitrate_kbps),
                "height": height,
                "bitrate_kbps": media.bitrate_kbps,
            }
        )
    own = min(height, 1080)
    names = [n for n, (h, kbps) in _STEPS.items() if h <= own and kbps < media.bitrate_kbps]
    if not any(_STEPS[n][0] == own for n in names):
        # An efficient codec's bitrate can be below every step of its class; the class stays.
        names.insert(0, [n for n, (h, _) in _STEPS.items() if h == own][-1])
    return rows + [_convert_row(n) for n in names]


def resolve_quality(rows: list[QualityRow], asked: str) -> str:
    """The quality to play for *asked*: itself when the rows have it, else the nearest lower one.

    The basic three always stand for themselves, so a page loaded earlier keeps working.
    """
    converts = [r for r in rows if r["kind"] == "convert"]
    if asked in _BASIC:
        return asked
    if asked == "auto":
        return next(
            (r["id"] for r in converts if (r["bitrate_kbps"] or 0) <= _AUTO_MAX_KBPS),
            converts[-1]["id"],
        )
    if any(r["id"] == asked for r in rows):
        return asked
    if asked == ORIGINAL:
        return converts[0]["id"]
    wanted = _STEPS[asked]
    return next(
        (r["id"] for r in converts if (r["height"] or 0, r["bitrate_kbps"] or 0) <= wanted),
        converts[-1]["id"],
    )


def without_original(rows: list[QualityRow]) -> list[QualityRow]:
    return [r for r in rows if r["kind"] != "original"]
