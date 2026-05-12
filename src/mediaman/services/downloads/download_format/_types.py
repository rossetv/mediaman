"""TypedDict definitions for the download format package."""

from __future__ import annotations

from typing import Any, TypedDict

#: Canonical state → user-visible label map. Single source of truth shared by
#: Jinja templates (via ``state_label`` field on the rendered item) and the
#: poll-loop JS (via the same field on the JSON response). Order matters
#: only as documentation; lookup is by key.
DOWNLOAD_STATE_LABELS: dict[str, str] = {
    "searching": "Looking for the best version",
    "queued": "Queued — waiting on indexer",
    "downloading": "Downloading",
    "almost_ready": "Almost ready",
    "ready": "Ready to watch",
    "upcoming": "",
}


def state_label(state: str) -> str:
    """Return the user-visible label for a download state, or the state itself if unknown."""
    return DOWNLOAD_STATE_LABELS.get(state, state)


class DownloadItem(TypedDict):
    """A single item on the downloads page, as returned by build_item()."""

    id: str
    title: str
    media_type: str
    poster_url: str
    state: str
    state_label: str
    progress: int
    eta: str
    size_done: str
    size_total: str
    # rationale: ``episodes`` is a free-shape display dict built by
    # ``build_episode_dicts`` with keys (label, title, state, progress,
    # is_pack_episode) whose presence and value types vary per row (e.g.
    # ``is_pack_episode`` is optional, ``progress`` is ``int | float``).
    # A TypedDict here would either be ``total=False`` (no tighter than
    # the current shape) or fail every test that builds a partial entry.
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
    stuck_seasons: list[dict]
    arr_id: int
    kind: str
