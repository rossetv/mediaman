"""Radarr status-projection helpers.

Pure projections from Radarr movie / queue payloads into the
``DownloadItem`` envelope. The orchestrator ``_radarr_status`` — which
builds the Arr client and decides which projection to apply — lives in the
package barrel (:mod:`mediaman.web.routes.download.status`) so the
``build_radarr_from_db`` call site stays patchable by the test suite.
"""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from mediaman.core.format import format_bytes
from mediaman.services.arr._types import ArrQueueItem, RadarrMovie
from mediaman.services.downloads.download_format import (
    build_item,
    extract_poster_url,
    map_arr_status,
)
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.web.repository.download import fetch_recent_download

from ._shared import _format_timeleft, _safe_int, _safe_progress


def _radarr_ready_item(movie: RadarrMovie) -> DownloadItem:
    """Build the ``state="ready"`` envelope for a movie already on disk."""
    title = movie.get("title", "")
    # rationale: ``RadarrMovie.images`` is typed as ``list[ArrImage]``,
    # but ``extract_poster_url`` accepts ``Sequence[Mapping[str, object]]``
    # to support the parallel Sonarr branches that pass through raw
    # queue dicts.  The runtime values are identical; the cast narrows
    # the mypy view without a runtime conversion.
    poster_url = extract_poster_url(cast("list[dict[Any, Any]] | None", movie.get("images")))
    return build_item(
        dl_id=f"radarr:{title}",
        title=title,
        media_type="movie",
        poster_url=poster_url,
        state="ready",
        progress=100,
        eta="",
        size_done="",
        size_total="",
    )


def _radarr_queue_item(queue: list[ArrQueueItem], tmdb_id: int) -> DownloadItem | None:
    """Find the queue entry for *tmdb_id* and project it into a download item.

    Returns ``None`` when the queue does not contain a matching item.
    """
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
        eta = _format_timeleft(item.get("timeleft", ""))
        if state == "almost_ready":
            eta = "Post-processing…"
        title = item_movie.get("title", "")
        poster_url = extract_poster_url(item_movie.get("images"))
        return build_item(
            dl_id=f"radarr:{title}",
            title=title,
            media_type="movie",
            poster_url=poster_url,
            state=state,
            progress=progress,
            eta=eta,
            size_done=format_bytes(size_total - size_left),
            size_total=format_bytes(size_total),
        )
    return None


def _radarr_fallback_item(conn: sqlite3.Connection, movie: RadarrMovie | None) -> DownloadItem:
    """Return a recent-download or searching envelope when the queue had no match.

    Falls back to a "ready" envelope built from a recently-recorded
    download row before settling on "searching" — preserves the
    after-import UX where a movie disappears from the queue between
    polls.
    """
    title = (movie or {}).get("title", "")
    if title:
        recent = fetch_recent_download(conn, f"radarr:{title}")
        if recent is not None:
            return build_item(
                dl_id=recent.dl_id,
                title=recent.title,
                media_type="movie",
                poster_url=recent.poster_url,
                state="ready",
                progress=100,
                eta="",
                size_done="",
                size_total="",
            )
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
