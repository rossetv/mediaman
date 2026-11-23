"""The card every Watch row and search group shows for a title, and the proxied TMDB
image addresses it uses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from mediaman.services.arr.state import ACTION_DOWNLOADING, ACTION_QUEUED

_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/"
_IMAGE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\.(?:jpg|jpeg|png|webp)")


def tmdb_image_url(path: object, size: str) -> str | None:
    """The Watch image-proxy address for a TMDB image, given its path ("/abc.jpg") or its
    full image.tmdb.org URL; None for anything else."""
    if not isinstance(path, str):
        return None
    if path.startswith(_TMDB_IMAGE_BASE):
        _size, sep, name = path[len(_TMDB_IMAGE_BASE) :].partition("/")
        path = sep + name
    name = path[1:] if path.startswith("/") else ""
    return f"/api/watch/img/tmdb/{size}/{name}" if _IMAGE_NAME_RE.fullmatch(name) else None


def library_href(library_ref: str | None) -> str | None:
    """The no-script page of a library title ref ("movie-<key>" or "show-<key>")."""
    if library_ref is None:
        return None
    kind, _, key = library_ref.partition("-")
    return f"/watch/{kind}/{key}" if kind in ("movie", "show") else None


def discover_card(
    *,
    ref: str,
    kind: Literal["movie", "show"],
    title: str,
    year: int | None,
    poster_url: str | None,
    rating: float | None,
    library_ref: str | None,
    download_state: str | None,
    download_pct: int | None,
) -> dict[str, object]:
    """One card of a discover row, a TMDB search result or a recommendation."""
    return {
        "ref": ref,
        "kind": kind,
        "title": title,
        "year": year,
        "poster_url": poster_url,
        "rating": rating,
        "in_library": library_ref is not None,
        "library_ref": library_ref,
        "href": library_href(library_ref),
        "download_state": download_state,
        "download_pct": (
            download_pct if download_state in (ACTION_QUEUED, ACTION_DOWNLOADING) else None
        ),
    }


def year_of(value: object) -> int | None:
    text = value if isinstance(value, str) else ""
    return int(text[:4]) if text[:4].isdigit() else None


def tmdb_rating(value: object) -> float | None:
    """TMDB's vote average to one decimal; None when it has no votes."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not value:
        return None
    return round(float(value), 1)


def tmdb_kind(item: Mapping[str, Any]) -> Literal["movie", "tv"] | None:
    media_type = item.get("media_type")
    return media_type if media_type in ("movie", "tv") else None


def tmdb_id_of(item: Mapping[str, Any]) -> int | None:
    value = item.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
