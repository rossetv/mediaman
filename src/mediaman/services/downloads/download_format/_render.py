"""Rendering helpers: build_item, build_episode_summary, select_hero."""

from __future__ import annotations

from mediaman.services.downloads.download_format._types import (
    DownloadItem,
    attention_hint,
    state_label,
)


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
    label: str = "",
    attention: str = "",
) -> DownloadItem:
    """Build a simplified download item for the API response.

    search_count and last_search_ts are set only for the searching state; added_at is
    the fallback before the first search (first 5 minutes, or after a restart).
    label is what to call it where its title does not tell it from another.
    attention is what Radarr or Sonarr says is wrong with one that needs attention.
    abandon_visible is a server-side threshold check so the frontend needs no logic.
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
        "label": label,
        "attention": attention if state == "needs_attention" else "",
        "attention_hint": attention_hint(arr_source) if state == "needs_attention" else "",
    }


def build_episode_summary(episodes: list[dict[str, object]]) -> str:
    total = len(episodes)
    ready = sum(1 for e in episodes if e["state"] == "ready")
    downloading = sum(1 for e in episodes if e["state"] == "downloading")
    queued = sum(1 for e in episodes if e["state"] == "queued")
    searching = sum(1 for e in episodes if e["state"] == "searching")
    held = sum(1 for e in episodes if e["state"] == "needs_attention")

    parts = []
    if ready:
        parts.append(f"{ready} of {total} episode{'s' if total != 1 else ''} ready")
    if downloading:
        parts.append(f"{downloading} episode{'s' if downloading != 1 else ''} downloading")
    if queued:
        parts.append(f"{queued} queued")
    if searching:
        parts.append(f"{searching} searching")
    if held:
        parts.append(f"{held} need{'s' if held == 1 else ''} attention")
    return " · ".join(parts)


_HERO_STATE_PRIORITY: dict[str, int] = {
    "downloading": 0,
    "almost_ready": 1,
    "needs_attention": 2,
    "queued": 3,
    "searching": 4,
    "upcoming": 5,
}


# A download that is coming in: the top card stays on it while it is.
_ACTIVE = frozenset({"downloading", "almost_ready"})


def select_hero(
    items: list[dict[str, object]], kept: str = ""
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    """Pick the hero item and return (hero, remaining_items).

    By state: downloading, almost_ready, needs_attention, queued, searching; within one, the oldest added
    first, then by title, so nothing moves while its state stands. The item of id *kept*, the
    last hero, stays the hero while it is downloading or almost ready.
    """
    if not items:
        return None, []

    def sort_key(item: dict[str, object]) -> tuple[int, float, str, str]:
        priority = _HERO_STATE_PRIORITY.get(str(item.get("state") or ""), 6)
        added_at = item.get("added_at")
        added = added_at if isinstance(added_at, int | float) else 0.0
        return (priority, added, str(item.get("title") or "").casefold(), str(item.get("id")))

    ranked = sorted(items, key=sort_key)
    stays = [i for i in ranked if kept and i.get("id") == kept and i.get("state") in _ACTIVE]
    hero = stays[0] if stays else ranked[0]
    return hero, [i for i in ranked if i is not hero]
