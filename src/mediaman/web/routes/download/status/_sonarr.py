"""Sonarr status projections: _sonarr_queue_entries, _sonarr_aggregate and the no-queue
_sonarr_series_fallback. The orchestrator lives in the package barrel so
build_sonarr_from_db stays patchable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

from mediaman.core.format import format_bytes
from mediaman.services.arr._types import ArrQueueItem, SonarrSeries
from mediaman.services.downloads.download_format import (
    build_episode_summary,
    build_item,
    extract_poster_url,
    format_episode_label,
    map_arr_status,
)
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.downloads.download_queue import build_episode_dicts

from ._shared import _format_timeleft, _safe_int, _safe_progress

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
    timeleft: str
    download_id: str


def _sonarr_queue_entries(
    queue: list[ArrQueueItem], tmdb_id: int, season_number: int | None = None
) -> tuple[str, str, list[_SonarrEpEntry]]:
    """Return (series_title, series_poster, ep_entries) from the first matching queue item;
    ep_entries has one accumulator per matching episode, of one season when given.
    """
    series_title = ""
    series_poster = ""
    ep_entries: list[_SonarrEpEntry] = []

    for item in queue:
        item_series = item.get("series") or {}
        episode = item.get("episode") or {}
        season_num = episode.get("seasonNumber")
        if item_series.get("tmdbId") != tmdb_id or season_number not in (None, season_num):
            continue

        if not series_title:
            series_title = item_series.get("title", "")
        if not series_poster:
            series_poster = extract_poster_url(item_series.get("images"))

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
                "timeleft": item.get("timeleft", ""),
                "download_id": item.get("downloadId") or "",
            }
        )

    return series_title, series_poster, ep_entries


def _sonarr_aggregate(
    series_title: str, series_poster: str, ep_entries: list[_SonarrEpEntry]
) -> DownloadItem:
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
    raw_statuses = [e["status"] for e in ep_entries]
    raw_tracked = [e["tracked_state"] for e in ep_entries]
    combined_status = next(
        (s for s in raw_statuses if s.lower() in ("downloading", "completed")),
        raw_statuses[0] if raw_statuses else "",
    )
    combined_tracked = next(
        (s for s in raw_tracked if s.lower() in ("downloading", "importing", "importpending")),
        raw_tracked[0] if raw_tracked else "",
    )
    state = map_arr_status(combined_status, combined_tracked)
    eta = _format_timeleft(max((e["timeleft"] for e in ep_entries if e["timeleft"]), default=""))
    if state == "almost_ready":
        eta = "Post-processing…"
    episode_summary = build_episode_summary(episodes)
    return build_item(
        dl_id=f"sonarr:{series_title}",
        title=series_title,
        media_type="series",
        poster_url=series_poster,
        state=state,
        progress=overall_progress,
        eta=eta,
        size_done=format_bytes(total_size - total_left),
        size_total=format_bytes(total_size),
        episodes=episodes,
        episode_summary=episode_summary,
    )


def _episode_files(series: SonarrSeries, season_number: int | None) -> int:
    if season_number is None:
        return (series.get("statistics") or {}).get("episodeFileCount", 0)
    return sum(
        (season.get("statistics") or {}).get("episodeFileCount", 0)
        for season in series.get("seasons") or []
        if season.get("seasonNumber") == season_number
    )


def _sonarr_series_fallback(
    client: ArrClient, tmdb_id: int, season_number: int | None = None
) -> DownloadItem:
    all_series = client.get_series()
    matched = next(
        (s for s in all_series if s.get("tmdbId") == tmdb_id),
        None,
    )
    if matched:
        s_title = matched.get("title", "")
        if _episode_files(matched, season_number) > 0:
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
