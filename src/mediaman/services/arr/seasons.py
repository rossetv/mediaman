"""What Sonarr's data says of each season of a show, and adding seasons to a show it tracks."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

import requests

from mediaman.services.arr._transport import ArrError, SearchUnanswered
from mediaman.services.arr._types import ArrQueueItem, ArrSeason, SonarrSeries
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.state import _season_has_aired, _season_stats, season_on_disk
from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)

QueueState = Literal["downloading", "queued"]
Status = Literal["held", "partly_held", "downloading", "queued", "asked", "not_asked", "not_out"]
#: (Sonarr series id, season number) -> what the download queue holds of it.
SeasonQueue = Mapping[tuple[int, int], QueueState]


@dataclass(frozen=True, slots=True)
class SeasonState:
    status: Status
    #: Sonarr, as set, fetches what is missing of it.
    asked: bool


def queued_seasons(queue: Iterable[ArrQueueItem]) -> dict[tuple[int, int], QueueState]:
    """What the queue is bringing of each season; an entry that brings nothing is left out, as
    in the download state of a show."""
    # Late import: download_format reaches back into this package.
    from mediaman.services.arr.fetcher._base import clamp_progress
    from mediaman.services.downloads.download_format import map_episode_state

    found: dict[tuple[int, int], QueueState] = {}
    for entry in queue:
        series_id = (entry.get("series") or {}).get("id")
        number = (entry.get("episode") or {}).get("seasonNumber")
        if not isinstance(series_id, int) or not isinstance(number, int):
            continue
        size, left = entry.get("size") or 0, entry.get("sizeleft") or 0
        state = map_episode_state(
            {
                "size": size,
                "sizeleft": left,
                "progress": clamp_progress(size, left),
                "status": entry.get("status") or entry.get("trackedDownloadStatus") or "queued",
            }
        )
        if state == "queued":
            found.setdefault((series_id, number), "queued")
        elif state != "searching":
            found[series_id, number] = "downloading"
    return found


def season_states(series: SonarrSeries, queue: SeasonQueue) -> dict[int, SeasonState]:
    """The state of each season Sonarr lists of *series*."""
    seasons = series.get("seasons") or []
    # A show just added has no episodes counted yet, which says nothing of what has aired.
    counted = any(_season_stats(s).get("totalEpisodeCount") for s in seasons)
    states = {}
    for season in seasons:
        number = season.get("seasonNumber")
        if number is None:
            continue
        in_queue = queue.get((series.get("id") or 0, number))
        states[number] = SeasonState(
            _status(series, season, in_queue, counted), is_asked(series, season)
        )
    return states


def is_asked(series: SonarrSeries, season: ArrSeason) -> bool:
    """Whether Sonarr fetches what is missing of the season: nothing of an unmonitored series."""
    return series.get("monitored") is not False and bool(season.get("monitored"))


def _status(
    series: SonarrSeries, season: ArrSeason, in_queue: QueueState | None, counted: bool
) -> Status:
    if season_on_disk(season):
        return "held"
    if in_queue is not None:
        return in_queue
    if _season_stats(season).get("episodeFileCount"):
        return "partly_held"
    if counted and not _season_has_aired(season):
        return "not_out"
    return "asked" if is_asked(series, season) else "not_asked"


def add_season(client: ArrClient, series_id: int, number: int) -> bool:
    """Monitor and search one season of a series Sonarr holds; whether it is monitored now.

    One monitored whose search could not be started counts: Sonarr fetches it, and mediaman's
    own passes search what stays missing. Raises when Sonarr cannot say what became of it.
    """
    try:
        client.remonitor_season(series_id, number)
    except SearchUnanswered:
        logger.warning("sonarr search of series_id=%s season=%s got no answer", series_id, number)
    except (SafeHTTPError, requests.RequestException, ValueError, ArrError):
        logger.warning(
            "sonarr add of series_id=%s season=%s failed", series_id, number, exc_info=True
        )
        series = client.get_series_by_id(series_id)
        return any(
            is_asked(series, s)
            for s in series.get("seasons") or []
            if s.get("seasonNumber") == number
        )
    return True
