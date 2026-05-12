"""Rendering helpers — build_item, build_episode_summary, select_hero."""

from __future__ import annotations

from mediaman.services.downloads.download_format._types import DownloadItem, state_label


def build_item(
    *,
    dl_id: str,
    title: str,
    media_type: str,
    poster_url: str,
    state: str,
    progress: int,
    eta: str,
    size_done: str,
    size_total: str,
    episodes: list[dict[str, object]] | None = None,
    episode_summary: str = "",
    release_label: str = "",
    has_pack: bool = False,
    search_count: int = 0,
    last_search_ts: float = 0.0,
    added_at: float = 0.0,
    search_hint: str = "",
    arr_link: str = "",
    arr_source: str = "",
    abandon_visible: bool = False,
    stuck_seasons: list[dict[str, int]] | None = None,
    arr_id: int = 0,
    kind: str = "",
) -> DownloadItem:
    """Build a simplified download item for the API response.

    ``search_count`` / ``last_search_ts`` are populated only for items in
    the ``searching`` state and power the "Last searched Xm ago" subline
    in the UI. ``added_at`` is used as a fallback when mediaman hasn't
    fired a search yet (first 5 min, or across a restart).

    ``arr_link`` is the deep-link URL into Radarr/Sonarr for the item,
    and ``arr_source`` is ``"Radarr"`` or ``"Sonarr"`` — used to label
    the deep-link button.

    ``abandon_visible`` is a server-authoritative threshold check so the
    frontend renders the abandon button without any client-side logic.
    ``stuck_seasons`` is a per-season missing-episode breakdown for series
    (empty for movies).
    """
    if stuck_seasons is None:
        stuck_seasons = []
    return {
        "id": dl_id,
        "title": title,
        "media_type": media_type,
        "poster_url": poster_url,
        "state": state,
        "state_label": state_label(state),
        "progress": progress,
        "eta": eta,
        "size_done": size_done,
        "size_total": size_total,
        "episodes": episodes,
        "episode_summary": episode_summary,
        "release_label": release_label,
        "has_pack": has_pack,
        "search_count": search_count,
        "last_search_ts": last_search_ts,
        "added_at": added_at,
        "search_hint": search_hint,
        "arr_link": arr_link,
        "arr_source": arr_source,
        "abandon_visible": abandon_visible,
        "stuck_seasons": stuck_seasons,
        "arr_id": arr_id,
        "kind": kind,
    }


def build_episode_summary(episodes: list[dict[str, object]]) -> str:
    """Build a human-readable summary like '2 of 8 episodes ready ...'."""
    total = len(episodes)
    ready = sum(1 for e in episodes if e["state"] == "ready")
    downloading = sum(1 for e in episodes if e["state"] == "downloading")
    queued = sum(1 for e in episodes if e["state"] == "queued")
    searching = sum(1 for e in episodes if e["state"] == "searching")

    parts = []
    if ready:
        parts.append(f"{ready} of {total} episodes ready")
    if downloading:
        parts.append(f"{downloading} downloading")
    if queued:
        parts.append(f"{queued} queued")
    if searching:
        parts.append(f"{searching} searching")
    return " · ".join(parts)


def select_hero(
    items: list[dict[str, object]],
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    """Pick the hero item from a list of download items.

    The actively downloading item with the highest progress becomes the hero.
    If nothing is downloading, the first item wins.
    Returns (hero, remaining_items).
    """
    if not items:
        return None, []
    if len(items) == 1:
        return items[0], []

    def sort_key(item):
        is_downloading = item["state"] == "downloading"
        return (not is_downloading, -item["progress"])

    ranked = sorted(items, key=sort_key)
    return ranked[0], ranked[1:]
