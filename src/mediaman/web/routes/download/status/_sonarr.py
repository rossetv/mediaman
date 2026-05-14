"""Sonarr status-projection helpers.

The Sonarr status projection has three independent phases, each broken out
into its own helper here:

  * :func:`_sonarr_queue_entries`  — walk the download queue, collecting the
    per-episode accumulators for the target series.
  * :func:`_sonarr_aggregate`      — fold those per-episode entries into a
    single multi-episode ``DownloadItem``.
  * :func:`_sonarr_series_fallback` — the no-queue path: ask ``get_series()``
    whether the series is already on disk, recently downloaded, or still
    being searched for.

The orchestrator ``_sonarr_status`` — which builds the Arr client and runs
the three phases in order — lives in the package barrel
(:mod:`mediaman.web.routes.download.status`) so the ``build_sonarr_from_db``
call site stays patchable by the test suite.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, TypedDict, cast

from mediaman.core.format import format_bytes
from mediaman.services.arr._types import ArrQueueItem
from mediaman.services.downloads.download_format import (
    build_episode_summary,
    build_item,
    extract_poster_url,
    format_episode_label,
    map_arr_status,
)
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.downloads.download_queue import build_episode_dicts
from mediaman.web.repository.download import fetch_recent_download

from ._shared import _format_timeleft, _safe_int, _safe_progress

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient


class _SonarrEpEntry(TypedDict):
    """Intermediate per-episode accumulator used inside ``_sonarr_status``."""

    label: str
    title: str
    progress: int
    size: int
    sizeleft: int
    status: str
    tracked_state: str
    timeleft: str


def _sonarr_queue_entries(
    queue: list[ArrQueueItem], tmdb_id: int
) -> tuple[str, str, list[_SonarrEpEntry]]:
    """Walk the Sonarr download *queue*, collecting entries for *tmdb_id*.

    Returns ``(series_title, series_poster, ep_entries)`` — the title and
    poster are taken from the first matching queue item, ``ep_entries`` is
    one accumulator per matching episode (empty when nothing matches).
    """
    series_title = ""
    series_poster = ""
    ep_entries: list[_SonarrEpEntry] = []

    for item in queue:
        item_series = item.get("series") or {}
        if item_series.get("tmdbId") != tmdb_id:
            continue

        if not series_title:
            series_title = item_series.get("title", "")
        if not series_poster:
            series_poster = extract_poster_url(item_series.get("images"))

        episode = item.get("episode") or {}
        size = _safe_int(item.get("size"))
        sizeleft = _safe_int(item.get("sizeleft"))
        ep_progress = _safe_progress(size, sizeleft) if size else 0
        season_num = episode.get("seasonNumber")
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
            }
        )

    return series_title, series_poster, ep_entries


def _sonarr_aggregate(
    series_title: str, series_poster: str, ep_entries: list[_SonarrEpEntry]
) -> DownloadItem:
    """Fold the per-episode *ep_entries* into one multi-episode download item.

    Callers must only invoke this with a non-empty ``ep_entries`` list — it
    is the queue-present branch of ``_sonarr_status``.
    """
    ep_entries.sort(key=lambda e: e["label"])
    episodes = build_episode_dicts(cast("list[dict[str, object]]", ep_entries))
    total_size = sum(e["size"] for e in ep_entries)
    total_left = sum(e["sizeleft"] for e in ep_entries)
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


def _sonarr_series_fallback(
    client: ArrClient, conn: sqlite3.Connection, tmdb_id: int
) -> DownloadItem:
    """Return the no-queue status for *tmdb_id* via ``client.get_series()``.

    Used when the download queue holds nothing for the series: classifies
    it as ``ready`` (already on disk, or a recent download row), or
    ``searching`` (matched but no files, or no match at all).
    """
    all_series = client.get_series()
    matched = next(
        (s for s in all_series if s.get("tmdbId") == tmdb_id),
        None,
    )
    if matched:
        stats = matched.get("statistics") or {}
        s_title = matched.get("title", "")
        if stats.get("episodeFileCount", 0) > 0:
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

        recent = fetch_recent_download(conn, f"sonarr:{s_title}")
        if recent is not None:
            return build_item(
                dl_id=recent.dl_id,
                title=recent.title,
                media_type="series",
                poster_url=recent.poster_url,
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
