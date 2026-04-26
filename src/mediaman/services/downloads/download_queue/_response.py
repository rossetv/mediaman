"""TypedDict for the downloads response."""

from __future__ import annotations

from typing import TypedDict


class DownloadsResponse(TypedDict):
    """Return type for :func:`build_downloads_response`."""

    hero: dict[str, object] | None
    queue: list[dict[str, object]]
    upcoming: list[dict[str, object]]
    recent: list[dict[str, object]]
