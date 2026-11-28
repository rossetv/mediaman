"""The copies of a title that Sonarr or Radarr find with an audio language: each release the
service would take, with the languages it names, best first by the service's own score."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from mediaman.services.cast.tracks import LANGUAGES, normalise_language

COPIES_MAX = 12
_GUID_MAX = 2048
_TEXT_MAX = 120

# Servarr's names that are not a language's English name in ``LANGUAGES``.
_ALIASES = {"flemish": "nl", "spanish (latino)": "es", "portuguese (brazil)": "pt"}
_BY_NAME = {name.lower(): code for code, name in LANGUAGES.items()} | _ALIASES


@dataclass(frozen=True)
class Copy:
    guid: str
    indexer_id: int
    languages: tuple[str, ...]
    quality: str
    size_bytes: int | None
    age_days: int | None
    indexer: str
    score: int
    season_pack: bool


def language_code(name: object) -> str | None:
    """The ISO 639 code of a language as Sonarr or Radarr name it; None for "Unknown",
    "Original" and any it does not know."""
    if not isinstance(name, str):
        return None
    lowered = name.strip().lower()
    code = _BY_NAME.get(lowered) or _BY_NAME.get(re.sub(r"\s*\(.*\)$", "", lowered))
    return normalise_language(code)


def _languages(release: Mapping[str, Any]) -> tuple[str, ...]:
    named = release.get("languages")
    codes: list[str] = []
    for entry in named if isinstance(named, list) else []:
        code = language_code(entry.get("name") if isinstance(entry, dict) else None)
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def _quality(release: Mapping[str, Any]) -> str:
    quality = release.get("quality")
    inner = quality.get("quality") if isinstance(quality, dict) else None
    name = inner.get("name") if isinstance(inner, dict) else None
    return name[:_TEXT_MAX] if isinstance(name, str) else ""


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def copies_with(releases: Iterable[Mapping[str, Any]], language: str) -> list[Copy]:
    """The releases that name *language* among their audio, best first. One the service
    rejects is left out: it would not take its place, as an upgrade, once downloaded."""
    found: list[Copy] = []
    for release in releases:
        guid, indexer_id = release.get("guid"), _int(release.get("indexerId"))
        languages = _languages(release)
        if (
            language not in languages
            or release.get("rejected") is True
            or release.get("downloadAllowed") is False
            or not isinstance(guid, str)
            or not 0 < len(guid) <= _GUID_MAX
            or indexer_id is None
        ):
            continue
        indexer = release.get("indexer")
        score = release.get("customFormatScore")
        found.append(
            Copy(
                guid=guid,
                indexer_id=indexer_id,
                languages=languages,
                quality=_quality(release),
                size_bytes=_int(release.get("size")),
                age_days=_int(release.get("age")),
                indexer=indexer[:_TEXT_MAX] if isinstance(indexer, str) else "",
                score=score if isinstance(score, int) and not isinstance(score, bool) else 0,
                season_pack=release.get("fullSeason") is True,
            )
        )
    found.sort(key=lambda c: (-c.score, -len(c.languages), c.age_days or 0))
    return found[:COPIES_MAX]
