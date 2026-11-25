"""What Sonarr's data says of each season of a show, and adding seasons to a show it tracks."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

from mediaman.core.time import now_utc
from mediaman.services.arr._transport import ArrError, SearchUnanswered
from mediaman.services.arr._types import ArrQueueItem, ArrSeason, SonarrSeries
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.state import _season_has_aired, _season_stats, season_on_disk

logger = logging.getLogger(__name__)

QueueState = Literal["downloading", "queued"]
Status = Literal["held", "partly_held", "downloading", "queued", "asked", "not_asked", "not_out"]
#: (Sonarr series id, season number) -> what the download queue holds of it.
SeasonQueue = Mapping[tuple[int, int], QueueState]

_COUNTS = ("episodeFileCount", "episodeCount", "totalEpisodeCount")


@dataclass(frozen=True, slots=True)
class SeasonState:
    status: Status
    #: Sonarr, as set, fetches what is missing of it.
    asked: bool


@dataclass(frozen=True, slots=True)
class ShowSeasons:
    states: dict[int, SeasonState]
    #: Whether every season Sonarr gave could be read, so that one not in ``states`` is not listed.
    whole: bool
    #: The numbers of the seasons flagged monitored, and whether the show is.
    monitored_seasons: frozenset[int]
    monitored: bool

    def started_with(self, number: int) -> set[int]:
        """The other seasons Sonarr starts fetching when season *number* is added: it fetches
        nothing of an unmonitored show, and adding one monitors the show."""
        return set() if self.monitored else set(self.monitored_seasons - {number})


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_readable(season: object) -> bool:
    if not isinstance(season, dict) or not _is_count(season.get("seasonNumber")):
        return False
    stats = season.get("statistics")
    return stats is None or (
        isinstance(stats, dict) and all(_is_count(stats.get(name, 0)) for name in _COUNTS)
    )


def readable_seasons(series: SonarrSeries) -> tuple[list[ArrSeason], bool]:
    """The seasons of *series* that have the shape they should, and whether all do."""
    seasons = series.get("seasons")
    if not isinstance(seasons, list):
        return [], seasons is None
    found = [s for s in seasons if _is_readable(s)]
    return found, len(found) == len(seasons)


def readable_queue(queue: Iterable[ArrQueueItem]) -> list[ArrQueueItem]:
    """The entries of the queue that have the shape they should; of one whose episode alone has
    not, the rest."""
    found: list[ArrQueueItem] = []
    for entry in queue:
        # Any: foreign JSON, not yet what its type says.
        raw = cast(dict[str, Any], entry)
        if not isinstance(raw.get("series") or {}, dict):
            continue
        if not all(_is_count(raw.get(name) or 0) for name in ("size", "sizeleft")):
            continue
        if not isinstance(raw.get("episode") or {}, dict):
            raw = {**raw, "episode": {}}
        found.append(cast(ArrQueueItem, raw))
    return found


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


def show_seasons(
    series: SonarrSeries, queue: SeasonQueue, air_dates: Mapping[int, str | None]
) -> ShowSeasons:
    """The state of each season Sonarr lists of *series*; *air_dates* is when each first airs
    as TMDB has it, by number."""
    seasons, whole = readable_seasons(series)
    # A show just added has no episodes counted yet, which says nothing of what has aired.
    counted = any(_season_stats(s).get("totalEpisodeCount") for s in seasons)
    series_id = series.get("id")
    states = {}
    for season in seasons:
        number = season["seasonNumber"]
        in_queue = queue.get((series_id, number)) if isinstance(series_id, int) else None
        airs = air_dates.get(number)
        states[number] = SeasonState(
            _status(series, season, in_queue, counted, airs), is_asked(series, season)
        )
    monitored = frozenset(s["seasonNumber"] for s in seasons if s.get("monitored"))
    return ShowSeasons(states, whole, monitored, series.get("monitored") is not False)


def is_asked(series: SonarrSeries, season: ArrSeason) -> bool:
    """Whether Sonarr fetches what is missing of the season: nothing of an unmonitored series."""
    return series.get("monitored") is not False and bool(season.get("monitored"))


def is_still_to_air(air_date: str | None) -> bool | None:
    """Whether what first airs on *air_date* ("2026-01-31") is still to air; None when unknown."""
    try:
        return date.fromisoformat(air_date or "") > now_utc().date()
    except ValueError:
        return None


def _status(
    series: SonarrSeries,
    season: ArrSeason,
    in_queue: QueueState | None,
    counted: bool,
    air_date: str | None,
) -> Status:
    if season_on_disk(season):
        return "held"
    if in_queue is not None:
        return in_queue
    if _season_stats(season).get("episodeFileCount"):
        return "partly_held"
    if counted and _is_not_out(season, air_date):
        return "not_out"
    return "asked" if is_asked(series, season) else "not_asked"


def _is_not_out(season: ArrSeason, air_date: str | None) -> bool:
    """Whether none of the season has aired. Sonarr's airing dates leave out what is not
    monitored, so of a season that is not they say nothing; TMDB's date does."""
    if _season_has_aired(season):
        return False
    still_to_air = is_still_to_air(air_date)
    if still_to_air is not None:
        return still_to_air
    stats = _season_stats(season)
    return not stats.get("totalEpisodeCount") or bool(
        season.get("monitored") and stats.get("nextAiring")
    )


class SeasonRefused(ArrError):
    """A season is not added for a reason *result* names, found once its series was read."""

    def __init__(self, result: str) -> None:
        self.result = result
        super().__init__(result)


def add_season(
    client: ArrClient, series_id: int, number: int, judge: Callable[[SonarrSeries], str | None]
) -> None:
    """Monitor and search one season of a series Sonarr holds.

    *judge* is asked of the series as it is read to be changed; what it answers is raised as
    ``SeasonRefused`` and nothing is changed. A search that got no answer may be running.
    """

    def guard(series: SonarrSeries) -> None:
        if (result := judge(series)) is not None:
            raise SeasonRefused(result)

    try:
        client.remonitor_season(series_id, number, guard=guard)
    except SearchUnanswered:
        logger.warning("sonarr search of series_id=%s season=%s got no answer", series_id, number)


def is_refused_start(
    started: Collection[int], refused: Collection[int], named: Collection[int], *, is_admin: bool
) -> bool:
    """Whether adding a season to an unmonitored show is refused for the other seasons *started*
    with it: for any in *refused*, and for any not *named* unless an admin asks."""
    return any(n in refused or (n not in named and not is_admin) for n in started)
