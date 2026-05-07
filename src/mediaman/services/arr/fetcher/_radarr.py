"""Radarr queue fetch logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediaman.services.arr.fetcher._base import (
    ArrCard,
    _iter_still_searching,
    clamp_progress,
    make_arr_card,
)

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient
from mediaman.core.time import parse_iso_utc
from mediaman.services.downloads.download_format import (
    classify_movie_upcoming,
    compute_movie_released_at,
    extract_poster_url,
)


def fetch_radarr_queue(client: ArrClient) -> list[ArrCard]:
    """Build Radarr download cards from an already-constructed client.

    Returns cards for queue entries plus monitored movies still searching.
    The inner loop over ``get_movies()`` keeps its own try/except so a
    failure there doesn't wipe out the queue entries we already have.
    """
    items: list[ArrCard] = []
    for q in client.get_queue():
        movie = q.get("movie") or {}
        size = q.get("size") or 0
        sizeleft = q.get("sizeleft") or 0
        # Clamp to [0, 100]: Radarr can briefly report ``sizeleft > size`` while
        # a torrent re-downloads or pads, which would otherwise produce a
        # negative percentage and break progress bars.
        progress = clamp_progress(size, sizeleft)
        status = q.get("status") or q.get("trackedDownloadStatus") or "queued"
        poster_url = extract_poster_url(movie.get("images"))
        m_title = movie.get("title") or q.get("title") or "Unknown"
        release_name = q.get("title") or ""
        items.append(
            make_arr_card(
                "movie",
                m_title,
                source="Radarr",
                year=movie.get("year"),
                poster_url=poster_url,
                progress=progress,
                size=size,
                sizeleft=sizeleft,
                timeleft=q.get("timeleft") or "",
                status=status,
                release_names=[release_name] if release_name else [],
            )
        )

    # Also include monitored movies still searching (not yet in queue).
    # ``_iter_still_searching`` owns the outer try/except so a transient
    # upstream failure doesn't discard the queue cards we already
    # collected, and so both fetchers share a single exception-list
    # contract.
    queue_title_years = {(i["title"], i.get("year")) for i in items if i.get("kind") == "movie"}
    for movie in _iter_still_searching(client.get_movies, service_label="Radarr"):
        m_title = movie.get("title", "")
        m_year = movie.get("year")
        if not movie.get("monitored"):
            continue
        if movie.get("hasFile"):
            continue
        if (m_title, m_year) in queue_title_years:
            continue

        is_upcoming, release_label = classify_movie_upcoming(movie)
        released_at = compute_movie_released_at(movie)

        added_at = 0.0
        added_dt = parse_iso_utc(movie.get("added", ""))
        if added_dt is not None:
            added_at = added_dt.timestamp()

        poster_url = extract_poster_url(movie.get("images"))

        items.append(
            make_arr_card(
                "movie",
                m_title,
                source="Radarr",
                poster_url=poster_url,
                arr_id=movie.get("id", 0),
                title_slug=movie.get("titleSlug", ""),
                added_at=added_at,
                released_at=released_at,
                is_upcoming=is_upcoming,
                release_label=release_label,
            )
        )
    return items
