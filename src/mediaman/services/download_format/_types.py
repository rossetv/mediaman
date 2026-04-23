"""TypedDict definitions for the download format package."""

from __future__ import annotations

from typing import TypedDict


class DownloadItem(TypedDict):
    """A single item on the downloads page, as returned by build_item()."""

    id: str
    title: str
    media_type: str
    poster_url: str
    state: str
    progress: int
    eta: str
    size_done: str
    size_total: str
    episodes: list[dict] | None
    episode_summary: str
    release_label: str
    has_pack: bool
    search_count: int
    last_search_ts: float
    added_at: float
    search_hint: str
    arr_link: str
    arr_source: str
