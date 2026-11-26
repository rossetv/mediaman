"""Sonarr status projections: _sonarr_queue_entries, _sonarr_aggregate and the no-queue
_sonarr_series_fallback. The orchestrator lives in the package barrel so
build_sonarr_from_db stays patchable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

from mediaman.core.format import format_bytes
from mediaman.services.arr._types import ArrQueueItem, SonarrSeries
from mediaman.services.arr.state import series_ready
from mediaman.services.downloads.download_format import (
    attention_reason,
    build_episode_summary,
    build_item,
    format_episode_label,
    map_queue_state,
    queue_attention,
)
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.downloads.download_queue import build_episode_dicts

from ._shared import _eta, _safe_int, _safe_progress

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient


class _SonarrEpEntry(TypedDict):
    label: str
    title: str
    progress: int
    size: int
    sizeleft: int
    status: str
    tracked_state: str
    tracked_status: str
    attention: str
    timeleft: str
    download_id: str


def _is_series(series: SonarrSeries, tmdb_id: int, tvdb_id: int | None) -> bool:
    """Whether *series* is the one asked for: by its TVDB id when Sonarr has no TMDB id for it."""
    if series.get("tmdbId"):
        return series.get("tmdbId") == tmdb_id
    return tvdb_id is not None and series.get("tvdbId") == tvdb_id


def _sonarr_queue_entries(
    queue: list[ArrQueueItem],
    tmdb_id: int,
    season_number: int | None = None,
    tvdb_id: int | None = None,
) -> tuple[str, list[_SonarrEpEntry]]:
    """Return (series_title, ep_entries) from the first matching queue item;
    ep_entries has one accumulator per matching episode, of one season when given.
    """
    series_title = ""
    ep_entries: list[_SonarrEpEntry] = []

    for item in queue:
        item_series = item.get("series") or {}
        episode = item.get("episode") or {}
        season_num = episode.get("seasonNumber")
        if not _is_series(item_series, tmdb_id, tvdb_id):
            continue
        if season_number not in (None, season_num):
            continue

        if not series_title:
            series_title = item_series.get("title", "")

        size = _safe_int(item.get("size"))
        sizeleft = _safe_int(item.get("sizeleft"))
        ep_progress = _safe_progress(size, sizeleft) if size else 0
        ep_num = episode.get("episodeNumber")
        ep_label = format_episode_label(season_num, ep_num)

        ep_entries.append(
            {
                "label": ep_label,
                "title": episode.get("title", ""),
                "progress": ep_progress,
                "size": size,
                "sizeleft": sizeleft,
                "status": item.get("status") or "",
                "tracked_state": item.get("trackedDownloadState") or "",
                "tracked_status": item.get("trackedDownloadStatus") or "",
                "attention": attention_reason(item),
                "timeleft": item.get("timeleft", ""),
                "download_id": item.get("downloadId") or "",
            }
        )

    return series_title, ep_entries


def _sonarr_aggregate(series_title: str, ep_entries: list[_SonarrEpEntry]) -> DownloadItem:
    """Fold ep_entries into one multi-episode item; callers must pass a non-empty list."""
    ep_entries.sort(key=lambda e: e["label"])
    episodes = build_episode_dicts(cast("list[dict[str, object]]", ep_entries))
    # Every episode of a season pack reports the whole pack's size, under one download id.
    sizes = {
        e["download_id"] or f"episode {i}": (e["size"], e["sizeleft"])
        for i, e in enumerate(ep_entries)
    }
    total_size = sum(size for size, _ in sizes.values())
    total_left = sum(left for _, left in sizes.values())
    overall_progress = _safe_progress(total_size, total_left) if total_size else 0
    state = map_queue_state(cast("list[dict[str, object]]", ep_entries))
    eta = _eta(state, max((e["timeleft"] for e in ep_entries if e["timeleft"]), default=""))
    episode_summary = build_episode_summary(episodes)
    return build_item(
        dl_id=f"sonarr:{series_title}",
        title=series_title,
        media_type="series",
        poster_url="",
        state=state,
        progress=overall_progress,
        eta=eta,
        size_done=format_bytes(total_size - total_left),
        size_total=format_bytes(total_size),
        episodes=episodes,
        episode_summary=episode_summary,
        arr_source="Sonarr",
        attention=queue_attention(cast("list[dict[str, object]]", ep_entries)),
    )


def _sonarr_series_fallback(
    client: ArrClient,
    tmdb_id: int,
    season_number: int | None = None,
    tvdb_id: int | None = None,
) -> DownloadItem:
    all_series = client.get_series()
    matched = next(
        (s for s in all_series if _is_series(s, tmdb_id, tvdb_id)),
        None,
    )
    if matched:
        s_title = matched.get("title", "")
        if series_ready(matched, season_number):
            return build_item(
                dl_id=f"sonarr:{s_title}",
                title=s_title,
                media_type="series",
                poster_url="",
                state="ready",
                progress=100,
                eta="",
                size_done="",
                size_total="",
            )

        return build_item(
            dl_id=f"sonarr:{s_title}",
            title=s_title,
            media_type="series",
            poster_url="",
            state="searching",
            progress=0,
            eta="",
            size_done="",
            size_total="",
        )

    return build_item(
        dl_id="",
        title="",
        media_type="series",
        poster_url="",
        state="searching",
        progress=0,
        eta="",
        size_done="",
        size_total="",
    )
