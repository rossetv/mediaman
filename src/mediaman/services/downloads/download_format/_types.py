"""TypedDict definitions for the download format package."""

from __future__ import annotations

from typing import Any, TypedDict


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
    episodes: list[dict[str, Any]] | None
    episode_summary: str
    release_label: str
    has_pack: bool
    search_count: int
    last_search_ts: float
    added_at: float
    search_hint: str
    arr_link: str
    arr_source: str
    abandon_visible: bool
    abandon_escalated: bool
    stuck_seasons: list[dict]
    arr_id: int
    kind: str
