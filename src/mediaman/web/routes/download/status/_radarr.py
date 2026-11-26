"""Pure Radarr movie/queue projections into DownloadItem. The orchestrator lives in the package
barrel so build_radarr_from_db stays patchable.
"""

from __future__ import annotations

from mediaman.core.format import format_bytes
from mediaman.services.arr._types import ArrQueueItem, RadarrMovie
from mediaman.services.downloads.download_format import (
    build_item,
    map_arr_status,
)
from mediaman.services.downloads.download_format._types import DownloadItem

from ._shared import _eta, _safe_int, _safe_progress


def _radarr_ready_item(movie: RadarrMovie) -> DownloadItem:
    title = movie.get("title", "")
    return build_item(
        dl_id=f"radarr:{title}",
        title=title,
        media_type="movie",
        poster_url="",
        state="ready",
        progress=100,
        eta="",
        size_done="",
        size_total="",
    )


def _radarr_queue_item(queue: list[ArrQueueItem], tmdb_id: int) -> DownloadItem | None:
    for item in queue:
        item_movie = item.get("movie") or {}
        if item_movie.get("tmdbId") != tmdb_id:
            continue
        size_left = _safe_int(item.get("sizeleft"))
        size_total = _safe_int(item.get("size"))
        progress = _safe_progress(size_total, size_left)
        state = map_arr_status(
            item.get("status") or "",
            item.get("trackedDownloadState") or "",
        )
        eta = _eta(state, item.get("timeleft", ""))
        title = item_movie.get("title", "")
        return build_item(
            dl_id=f"radarr:{title}",
            title=title,
            media_type="movie",
            poster_url="",
            state=state,
            progress=progress,
            eta=eta,
            size_done=format_bytes(size_total - size_left),
            size_total=format_bytes(size_total),
        )
    return None


def _radarr_searching_item(movie: RadarrMovie | None) -> DownloadItem:
    title = (movie or {}).get("title", "")
    return build_item(
        dl_id=f"radarr:{title}" if title else "",
        title=title,
        media_type="movie",
        poster_url="",
        state="searching",
        progress=0,
        eta="",
        size_done="",
        size_total="",
    )
