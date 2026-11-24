"""Download-card item builders and NZBGet matching helpers, including the DownloadsResponse
TypedDict. Matching and construction work on the same ArrCard/NZB pair but are
independent of queue orchestration, so they live apart from it.
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


class DownloadsResponse(TypedDict):
    hero: dict[str, object] | None
    queue: list[dict[str, object]]
    upcoming: list[dict[str, object]]
    recent: list[dict[str, object]]
    subtitle: str


def _contains_word_run(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def nzb_matches_arr(nzb_t_norm: str, arr_candidates: list[str]) -> bool:
    """Return True if *nzb_t_norm* matches any of *arr_candidates*: one side's words
    must appear as a contiguous run of whole words in the other (normalised strings from
    the arr item's title and recorded release names), so "up" never matches "superman".
    """
    nzb_words = nzb_t_norm.split()
    if not nzb_words:
        return False
    for cand in arr_candidates:
        cand_words = cand.split()
        if cand_words and (
            _contains_word_run(cand_words, nzb_words) or _contains_word_run(nzb_words, cand_words)
        ):
            return True
    return False


def _stuck_seasons_from_episodes(
    episodes: Sequence[Mapping[str, object]],
) -> list[dict[str, int]]:
    """Group queue episodes by season and count missing ones, ascending, as {"number",
    "missing_episodes"} dicts. Episodes without a season_number count as season 0
    (Sonarr specials).
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
    matched_nzb: Mapping[str, object],
    state: str,
    eta: str,
    download_rate: int,
) -> DownloadItem:
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


def _derive_series_state(episodes: list[dict[str, object]]) -> str:
    if episodes and all(e["state"] == "ready" for e in episodes):
        return "almost_ready"
    if any(e["state"] in ("downloading", "queued") for e in episodes):
        return "downloading"
    return map_state(None, has_nzbget_match=False)


def _build_unmatched_series_item(
    arr: ArrCard,
    arr_base_urls: dict[str, str],
    build_search_hint: Callable[..., str],
    build_arr_link: Callable[[ArrCard, dict[str, str]], str],
    search_count: int,
    last_search_ts: float,
    added_at: float,
    abandon_visible_now: bool,
) -> DownloadItem:
    episodes = build_episode_dicts(arr.get("episodes", []))
    state = _derive_series_state(episodes)
    search_hint = (
        build_search_hint(
            search_count, last_search_ts, added_at, time.time(), dl_id=arr.get("dl_id", "")
        )
        if state == "searching"
        else ""
    )
    raw_episodes = arr.get("episodes", [])
    stuck_seasons = (
        arr.get("stuck_seasons") or _stuck_seasons_from_episodes(raw_episodes)
        if state == "searching"
        else []
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
        # Without a season there is nothing Abandon could unmonitor.
        abandon_visible=(state == "searching" and abandon_visible_now and bool(stuck_seasons)),
        stuck_seasons=stuck_seasons,
        arr_id=arr.get("arr_id") or 0,
        kind="series",
    )


def _build_unmatched_movie_item(
    arr: ArrCard,
    arr_base_urls: dict[str, str],
    build_search_hint: Callable[..., str],
    build_arr_link: Callable[[ArrCard, dict[str, str]], str],
    search_count: int,
    last_search_ts: float,
    added_at: float,
    abandon_visible_now: bool,
) -> DownloadItem:
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


def build_unmatched_arr_item(
    arr: ArrCard,
    arr_base_urls: dict[str, str],
    build_search_hint: Callable[..., str],
    build_arr_link: Callable[[ArrCard, dict[str, str]], str],
) -> DownloadItem:
    """Build a card for an *arr entry with no NZBGet match, deriving state from episode
    progress (series) or percentage (movie).

    abandon_visible turns True once the item has been searching for at least
    _ABANDON_BUTTON_VISIBLE_AFTER_SECONDS (10 hours by default) since added_at.
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
        return _build_unmatched_series_item(
            arr,
            arr_base_urls,
            build_search_hint,
            build_arr_link,
            search_count,
            last_search_ts,
            added_at,
            abandon_visible_now,
        )
    return _build_unmatched_movie_item(
        arr,
        arr_base_urls,
        build_search_hint,
        build_arr_link,
        search_count,
        last_search_ts,
        added_at,
        abandon_visible_now,
    )
