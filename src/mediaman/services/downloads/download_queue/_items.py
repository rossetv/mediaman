"""Download-card item builders for matched and unmatched arr entries."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from mediaman.services.arr.fetcher._base import ArrCard, ArrEpisodeEntry
from mediaman.services.downloads.download_format import (
    build_episode_summary,
    build_item,
    map_episode_state,
    map_state,
)
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.infra.format import format_bytes

logger = logging.getLogger("mediaman")


def read_abandon_thresholds(conn) -> tuple[int, int]:
    """Return ``(visible_at, escalate_at)`` for the abandon-search button.

    Read once per ``build_downloads_response`` call and threaded down into
    every item builder, so a queue of N searching items pays one pair of
    SELECTs rather than 2N.
    """
    from mediaman.services.infra.settings_reader import get_int_setting

    try:
        visible_at = get_int_setting(
            conn, "abandon_search_visible_at", default=10, min=1, max=10000
        )
    except Exception:
        visible_at = 10
    try:
        escalate_at = get_int_setting(
            conn, "abandon_search_escalate_at", default=50, min=2, max=10000
        )
    except Exception:
        escalate_at = 50
    return visible_at, escalate_at


def _stuck_seasons_from_episodes(episodes: list[dict]) -> list[dict]:
    """Group queue episodes by season_number and count missing episodes.

    Returns a sorted list of ``{"number": int, "missing_episodes": int}``
    dicts, ascending by season number. Episodes without a season_number
    are grouped as season 0 — Sonarr uses 0 for specials.
    """
    by_season: dict[int, int] = {}
    for ep in episodes:
        s = int(ep.get("season_number") or 0)
        by_season[s] = by_season.get(s, 0) + 1
    return [
        {"number": s, "missing_episodes": n}
        for s, n in sorted(by_season.items())
    ]


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


def build_matched_item(
    arr: ArrCard,
    matched_nzb: dict[str, Any],
    state: str,
    eta: str,
    download_rate: int,
) -> DownloadItem:
    """Build a download-card item for an *arr entry that matched an NZBGet item."""
    if arr.get("kind") == "series":
        episodes = build_episode_dicts(arr.get("episodes", []))
        return build_item(
            dl_id=arr.get("dl_id", matched_nzb["dl_id"]),
            title=arr.get("title") or matched_nzb["title"],
            media_type="series",
            poster_url=arr.get("poster_url") or "",
            state=state,
            progress=arr.get("progress", matched_nzb["progress"]),
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
        dl_id=arr.get("dl_id", matched_nzb["dl_id"]),
        title=arr.get("title") or matched_nzb["title"],
        media_type="movie",
        poster_url=arr.get("poster_url") or "",
        state=state,
        progress=matched_nzb["progress"],
        eta=eta,
        size_done=format_bytes(matched_nzb["done_mb"] * 1024 * 1024),
        size_total=format_bytes(matched_nzb["file_mb"] * 1024 * 1024),
        arr_id=arr.get("arr_id") or 0,
        kind="movie",
    )


def build_unmatched_arr_item(
    arr: ArrCard,
    arr_base_urls: dict[str, str],
    build_search_hint: Callable[..., str],
    build_arr_link: Callable[[ArrCard, dict[str, str]], str],
    abandon_thresholds: tuple[int, int],
) -> DownloadItem:
    """Build a download-card item for an *arr entry with no NZBGet match.

    Derives the card state from episode progress (series) or reported
    percentage (movie) so callers don't need kind-specific logic.

    ``abandon_thresholds`` is the ``(visible_at, escalate_at)`` tuple read
    once at response-build time by :func:`read_abandon_thresholds`.
    """
    from mediaman.services.arr.search_trigger import get_search_info

    search_count, last_search_ts = get_search_info(arr.get("dl_id", ""))
    added_at = arr.get("added_at", 0.0)
    threshold, escalate_at = abandon_thresholds
    if arr.get("kind") == "series":
        episodes = build_episode_dicts(arr.get("episodes", []))
        if episodes and all(e["state"] == "ready" for e in episodes):
            state = "almost_ready"
        elif any(e["state"] in ("downloading", "queued") for e in episodes):
            state = "downloading"
        else:
            state = map_state(None, has_nzbget_match=False)
        search_hint = (
            build_search_hint(search_count, last_search_ts, added_at, time.time())
            if state == "searching"
            else ""
        )
        raw_episodes = arr.get("episodes", [])
        stuck_seasons = (
            _stuck_seasons_from_episodes(raw_episodes) if state == "searching" else []
        )
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
            abandon_visible=(state == "searching" and search_count >= threshold),
            abandon_escalated=(state == "searching" and search_count >= escalate_at),
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
        build_search_hint(search_count, last_search_ts, added_at, time.time())
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
        abandon_visible=(state == "searching" and search_count >= threshold),
        abandon_escalated=(state == "searching" and search_count >= escalate_at),
        stuck_seasons=[],
        arr_id=arr.get("arr_id") or 0,
        kind="movie",
    )
