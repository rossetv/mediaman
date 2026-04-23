"""TypedDict for the downloads response."""

from __future__ import annotations

from typing import TypedDict


class DownloadsResponse(TypedDict):
    """Return type for :func:`build_downloads_response`."""

    hero: dict | None
    queue: list
    upcoming: list
    recent: list
