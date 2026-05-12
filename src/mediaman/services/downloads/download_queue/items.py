"""Download-card item builders and NZBGet matching helpers.

WHAT: Builds individual download-card items (matched and unmatched Arr entries),
      maps episode data to display dicts, and tests NZBGet titles against Arr
      candidates. Also defines the ``DownloadsResponse`` TypedDict that is the
      return type of the orchestrator.

WHY: Item construction and NZBGet matching are tightly coupled — both operate on
     the same ArrCard/NZB pair — but are entirely independent of the queue
     orchestration lifecycle. Keeping them here lets queue.py stay thin.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TypedDict

from mediaman.core.format import format_bytes
from mediaman.services.arr.fetcher._base import ArrCard, ArrEpisodeEntry
from mediaman.services.downloads.download_format import (
    build_episode_summary,
    build_item,
    map_episode_state,
    map_state,
)
from mediaman.services.downloads.download_format._types import DownloadItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response TypedDict
# ---------------------------------------------------------------------------


class DownloadsResponse(TypedDict):
    """Return type for :func:`~mediaman.services.downloads.download_queue.queue.build_downloads_response`."""

    hero: dict[str, object] | None
    queue: list[dict[str, object]]
    upcoming: list[dict[str, object]]
    recent: list[dict[str, object]]


# ---------------------------------------------------------------------------
# NZBGet matching
# ---------------------------------------------------------------------------


def nzb_matches_arr(nzb_t_norm: str, arr_candidates: list[str]) -> bool:
    """Return True if *nzb_t_norm* matches any candidate in *arr_candidates*.

    Performs a bidirectional substring test so both "married at first sight
    au" ⊂ longer NZB titles and the reverse work correctly.  *arr_candidates*
    is a list of normalised strings built from the arr item's primary title
    and any release names Sonarr/Radarr recorded.
    """
    return any(cand in nzb_t_norm or nzb_t_norm in cand for cand in arr_candidates)


# ---------------------------------------------------------------------------
# Episode helpers
# ---------------------------------------------------------------------------


def _stuck_seasons_from_episodes(
    episodes: Sequence[Mapping[str, object]],
) -> list[dict[str, int]]:
    """Group queue episodes by season_number and count missing episodes.

    Returns a sorted list of ``{"number": int, "missing_episodes": int}``
    dicts, ascending by season number. Episodes without a season_number
    are grouped as season 0 — Sonarr uses 0 for specials.
    """
    by_season: dict[int, int] = {}
    for ep in episodes:
        raw = ep.get("season_number") or 0
        s = int(raw) if isinstance(raw, int | float | str) else 0
        by_season[s] = by_season.get(s, 0) + 1
    return [{"number": s, "missing_episodes": n} for s, n in sorted(by_season.items())]


def build_episode_dicts(
    eps_raw: list[ArrEpisodeEntry] | list[dict[str, object]],
) -> list[dict[str, object]]:
    """Map raw Sonarr episode entries to simplified display dicts.

    Used in both the NZBGet-matched and unmatched branches so the two
    paths produce identical episode structures.
    """
    return [
        {
            "label": e.get("label", ""),
            "title": e.get("title", ""),
            "state": map_episode_state(e),
            "progress": e.get("progress", 0),
            "is_pack_episode": e.get("is_pack_episode", False),
        }
        for e in eps_raw
    ]


# ---------------------------------------------------------------------------
# Item builders
# ---------------------------------------------------------------------------


def build_matched_item(
    arr: ArrCard,
    matched_nzb: Mapping[str, object],
    state: str,
    eta: str,
    download_rate: int,
) -> DownloadItem:
    """Build a download-card item for an *arr entry that matched an NZBGet item."""
    # The NZBGet matched-entry dict is constructed by ``parse_nzb_queue`` in
    # queue.py with values typed as ``object``; coerce each field to its
    # expected runtime type at the boundary so downstream code stays typed.
    nzb_dl_id = matched_nzb["dl_id"] if isinstance(matched_nzb["dl_id"], str) else ""
    nzb_title = matched_nzb["title"] if isinstance(matched_nzb["title"], str) else ""
    nzb_progress = (
        int(matched_nzb["progress"]) if isinstance(matched_nzb["progress"], int | float) else 0
    )
    nzb_done_mb = (
        float(matched_nzb["done_mb"]) if isinstance(matched_nzb["done_mb"], int | float) else 0.0
    )
    nzb_file_mb = (
        float(matched_nzb["file_mb"]) if isinstance(matched_nzb["file_mb"], int | float) else 0.0
    )
    if arr.get("kind") == "series":
        episodes = build_episode_dicts(arr.get("episodes", []))
        return build_item(
            dl_id=arr.get("dl_id", nzb_dl_id),
            title=arr.get("title") or nzb_title,
            media_type="series",
            poster_url=arr.get("poster_url") or "",
            state=state,
            progress=arr.get("progress", nzb_progress),
            eta=eta,
            size_done=arr.get("done_str", ""),
            size_total=arr.get("size_str", ""),
            episodes=episodes,
            episode_summary=build_episode_summary(episodes),
            has_pack=arr.get("has_pack", False),
            arr_id=arr.get("arr_id") or 0,
            kind="series",
        )
    return build_item(
        dl_id=arr.get("dl_id", nzb_dl_id),
        title=arr.get("title") or nzb_title,
        media_type="movie",
        poster_url=arr.get("poster_url") or "",
        state=state,
        progress=nzb_progress,
        eta=eta,
        size_done=format_bytes(int(nzb_done_mb * 1024 * 1024)),
        size_total=format_bytes(int(nzb_file_mb * 1024 * 1024)),
        arr_id=arr.get("arr_id") or 0,
        kind="movie",
    )


def build_unmatched_arr_item(
    arr: ArrCard,
    arr_base_urls: dict[str, str],
    build_search_hint: Callable[..., str],
    build_arr_link: Callable[[ArrCard, dict[str, str]], str],
) -> DownloadItem:
    """Build a download-card item for an *arr entry with no NZBGet match.

    Derives the card state from episode progress (series) or reported
    percentage (movie) so callers don't need kind-specific logic.

    ``abandon_visible`` becomes True once the item has been searching for
    at least :data:`~mediaman.services.arr.auto_abandon._ABANDON_BUTTON_VISIBLE_AFTER_SECONDS`
    seconds (10 hours by default), measured against ``added_at``.
    """
    from mediaman.services.arr.auto_abandon import _ABANDON_BUTTON_VISIBLE_AFTER_SECONDS
    from mediaman.services.arr.search_trigger import get_search_info

    search_count, last_search_ts = get_search_info(arr.get("dl_id", ""))
    added_at = arr.get("added_at", 0.0)
    now = time.time()
    # Guard: added_at=0 means the timestamp is missing; now - 0 ≈ 1.7e9 s
    # which would make every such item look like it has been searching for
    # years, showing the Abandon button immediately.
    abandon_visible_now = added_at > 0.0 and now - added_at >= _ABANDON_BUTTON_VISIBLE_AFTER_SECONDS
    if arr.get("kind") == "series":
        episodes = build_episode_dicts(arr.get("episodes", []))
        if episodes and all(e["state"] == "ready" for e in episodes):
            state = "almost_ready"
        elif any(e["state"] in ("downloading", "queued") for e in episodes):
            state = "downloading"
        else:
            state = map_state(None, has_nzbget_match=False)
        search_hint = (
            build_search_hint(
                search_count, last_search_ts, added_at, time.time(), dl_id=arr.get("dl_id", "")
            )
            if state == "searching"
            else ""
        )
        raw_episodes = arr.get("episodes", [])
        stuck_seasons = _stuck_seasons_from_episodes(raw_episodes) if state == "searching" else []
        return build_item(
            dl_id=arr.get("dl_id", ""),
            title=arr.get("title", "Unknown"),
            media_type="series",
            poster_url=arr.get("poster_url", ""),
            state=state,
            progress=arr.get("progress", 0),
            eta="Post-processing…" if state == "almost_ready" else "",
            size_done=arr.get("done_str", ""),
            size_total=arr.get("size_str", ""),
            episodes=episodes,
            episode_summary=build_episode_summary(episodes),
            has_pack=arr.get("has_pack", False),
            search_count=search_count,
            last_search_ts=last_search_ts,
            added_at=added_at,
            search_hint=search_hint,
            arr_link=build_arr_link(arr, arr_base_urls),
            arr_source=arr.get("source", ""),
            abandon_visible=(state == "searching" and abandon_visible_now),
            stuck_seasons=stuck_seasons,
            arr_id=arr.get("arr_id") or 0,
            kind="series",
        )
    state = (
        "almost_ready"
        if (arr.get("progress") or 0) >= 100
        else map_state(None, has_nzbget_match=False)
    )
    search_hint = (
        build_search_hint(
            search_count, last_search_ts, added_at, time.time(), dl_id=arr.get("dl_id", "")
        )
        if state == "searching"
        else ""
    )
    return build_item(
        dl_id=arr.get("dl_id", ""),
        title=arr.get("title", "Unknown"),
        media_type="movie",
        poster_url=arr.get("poster_url", ""),
        state=state,
        progress=arr.get("progress", 0),
        eta="Post-processing…" if state == "almost_ready" else "",
        size_done=arr.get("done_str", "0 B"),
        size_total=arr.get("size_str", "0 B"),
        search_count=search_count,
        last_search_ts=last_search_ts,
        added_at=added_at,
        search_hint=search_hint,
        arr_link=build_arr_link(arr, arr_base_urls),
        arr_source=arr.get("source", ""),
        abandon_visible=(state == "searching" and abandon_visible_now),
        stuck_seasons=[],
        arr_id=arr.get("arr_id") or 0,
        kind="movie",
    )
