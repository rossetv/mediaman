"""Classification and state-mapping helpers for the downloads page."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from mediaman.core.format import format_day_month
from mediaman.core.time import now_utc, parse_iso_utc

# rationale: these helpers consume *arr response shapes that are described
# by TypedDicts in mediaman.services.arr._types, but mypy treats a
# `TypedDict` value as not assignable to a `dict[str, object]` parameter.
# Annotating as `Mapping[str, object]` accepts both the TypedDicts and any
# plain dict the tests build, without falling back to `dict[Any, Any]`.
_ArrLike = Mapping[str, object]

# Maximum number of years into the future that a release date is trusted.
# TMDB sometimes stores year 9999 for unreleased titles — such values should
# not be surfaced as "Releases in 7973 years", so anything beyond this
# threshold is treated as "no valid date". 100 years comfortably covers
# legitimate announced-but-undated entries while still rejecting the 9999
# sentinel.
_MAX_FUTURE_YEARS = 100

# Radarr takes a film that has no date but the cinemas' for released this long after it.
_OUT_AFTER_CINEMAS = timedelta(days=90)

logger = logging.getLogger(__name__)


def extract_poster_url(images: Sequence[_ArrLike] | None) -> str:
    """Return the first poster remoteUrl from an *arr images list, or "" when there is none
    or *images* is falsy.
    """
    for img in images or []:
        remote_url = img.get("remoteUrl")
        if img.get("coverType") == "poster" and isinstance(remote_url, str) and remote_url:
            return remote_url
    return ""


def _format_release_date(dt: datetime) -> str:
    """Format as '14 Jun 2099' via format_day_month; %-d is a GNU-only strftime extension."""
    return format_day_month(dt)


def _release_dates(movie: _ArrLike, *keys: str) -> list[datetime]:
    """The dates of *keys* that parse, without the year-9999 sentinel and other absurd ones."""
    max_year = now_utc().year + _MAX_FUTURE_YEARS
    parsed = [parse_iso_utc(raw if isinstance(raw := movie.get(key), str) else "") for key in keys]
    return [dt for dt in parsed if dt and dt.year <= max_year]


def _awaited_releases(movie: _ArrLike) -> list[datetime]:
    """The dates of the releases Radarr waits for, by the film's minimumAvailability: none for
    "announced", or for "tba", which it takes one it was not told for."""
    home = _release_dates(movie, "digitalRelease", "physicalRelease")
    cinemas = _release_dates(movie, "inCinemas")
    waits_for = movie.get("minimumAvailability")
    if waits_for in ("released", "preDB") or (waits_for == "inCinemas" and not cinemas):
        return home
    return cinemas if waits_for == "inCinemas" else home + cinemas


def compute_movie_released_at(movie: _ArrLike) -> float:
    """Return the epoch of the earliest release Radarr waits for, or 0.0.

    Auto-abandon treats 0.0 as release date unknown and skips the item. Future dates are
    accepted.
    """
    awaited = _awaited_releases(movie) or [
        dt + _OUT_AFTER_CINEMAS for dt in _release_dates(movie, "inCinemas")
    ]
    return min(awaited).timestamp() if awaited else 0.0


def compute_series_released_at(episodes: Sequence[_ArrLike]) -> float:
    """Return the most recent past airing epoch across *episodes*, or 0.0.

    Auto-abandon uses it to avoid abandoning fresh searches. The latest past airing
    matters, not the earliest: an old series can have a missing episode that aired last
    week. Future airings are left to classify_series_upcoming.
    """
    now = now_utc()
    latest: datetime | None = None
    for ep in episodes:
        raw = ep.get("airDateUtc", "")
        dt = parse_iso_utc(raw if isinstance(raw, str) else "")
        if dt is None or dt > now:
            continue
        if latest is None or dt > latest:
            latest = dt
    if latest is None:
        return 0.0
    return latest.timestamp()


def next_movie_release(movie: _ArrLike) -> datetime | None:
    """The earliest release still to come of those Radarr waits for, or None when unknown."""
    now = now_utc()
    return min((dt for dt in _awaited_releases(movie) if now < dt), default=None)


def classify_movie_upcoming(movie: _ArrLike) -> tuple[bool, str]:
    if not movie.get("monitored"):
        return False, ""
    if movie.get("hasFile"):
        return False, ""
    if movie.get("isAvailable"):
        return False, ""

    released = next_movie_release(movie)
    if released is not None:
        return True, f"Releases {_format_release_date(released)}"
    return True, "Not yet released"


def classify_series_upcoming(
    series: _ArrLike, episodes: Sequence[_ArrLike], *, none_aired: bool = False
) -> tuple[bool, str]:
    """Classify a Sonarr series as upcoming and build its premiere label.

    *episodes* are those Sonarr is set to fetch; *none_aired* says it lists episodes of the
    series and none of a monitored season has aired, which makes it upcoming with no date
    too, unless it has ended. Returns (is_upcoming, release_label), label "" when not
    upcoming. Episodes with a missing or unparseable airDateUtc go into an unknown-airdate
    bucket and are counted at DEBUG rather than silently dropped.
    """
    if not series.get("monitored"):
        return False, ""
    stats_raw = series.get("statistics") or {}
    stats = stats_raw if isinstance(stats_raw, Mapping) else {}
    file_count = stats.get("episodeFileCount", 0)
    if isinstance(file_count, int) and file_count > 0:
        return False, ""

    now = now_utc()
    status_raw = series.get("status") or ""
    status = status_raw.lower() if isinstance(status_raw, str) else ""

    has_aired = False
    future_airs: list[datetime] = []
    unknown_count = 0

    for e in episodes:
        raw = e.get("airDateUtc", "")
        if not isinstance(raw, str) or not raw:
            unknown_count += 1
            continue
        dt = parse_iso_utc(raw)
        if dt is None:
            unknown_count += 1
            continue
        if dt < now:
            has_aired = True
        else:
            future_airs.append(dt)

    if unknown_count:
        logger.debug(
            "classify_series_upcoming: series=%r unknown_airdate_count=%d -- "
            "episodes with unparseable airDateUtc are in the unknown bucket",
            series.get("title", ""),
            unknown_count,
        )

    if status == "upcoming":
        if future_airs:
            return True, f"Premieres {_format_release_date(min(future_airs))}"
        return True, "Not yet aired"

    if future_airs and not has_aired:
        return True, f"Premieres {_format_release_date(min(future_airs))}"

    if none_aired and not has_aired and status != "ended":
        return True, "Not yet aired"

    return False, ""


_NZBGET_POST_PROCESSING = (
    "PP_",
    "LOADING_PARS",
    "VERIFYING",
    "REPAIRING",
    "RENAMING",
    "UNPACKING",
    "MOVING",
    "EXECUTING_SCRIPT",
)


def map_state(nzbget_status: str | None, has_nzbget_match: bool) -> str:
    """The state of a download NZBGet holds, by its NZBGet status."""
    if not has_nzbget_match or nzbget_status is None:
        return "searching"
    return map_arr_status(nzbget_status)


def map_arr_status(status: str, tracked_state: str = "") -> str:
    """The one mapping of a download's status to its state, for an NZBGet status or a
    Radarr/Sonarr queue item, whose status and trackedDownloadState are both read because
    versions populate them differently. One waiting its turn or paused is "queued".
    """
    lower = (status or "").lower()
    tracked = (tracked_state or "").lower()

    if tracked in ("importing", "importpending", "imported"):
        return "almost_ready"
    if lower == "completed" or any(stage.lower() in lower for stage in _NZBGET_POST_PROCESSING):
        return "almost_ready"
    # Failed, or held by no download client: nothing is coming in, whatever is tracked.
    if lower in ("failed", "downloadclientunavailable"):
        return "searching"
    # Radarr and Sonarr track as downloading what their download client holds but has not begun.
    if lower in ("queued", "paused", "delay"):
        return "queued"
    if lower in ("downloading", "fetching") or tracked == "downloading":
        return "downloading"
    return "searching"


def map_queue_state(entries: Sequence[_ArrLike]) -> str:
    """The state of a download of Radarr's or Sonarr's queue entries, a film's one or a show's
    episodes, each with its "status" and "tracked_state": by the one mapping, of the entry
    furthest along."""
    statuses = [str(e.get("status") or "") for e in entries]
    tracked = [str(e.get("tracked_state") or "") for e in entries]
    status = next(
        (s for s in statuses if s.lower() in ("downloading", "completed")),
        statuses[0] if statuses else "",
    )
    state = next(
        (t for t in tracked if t.lower() in ("downloading", "importing", "importpending")),
        tracked[0] if tracked else "",
    )
    return map_arr_status(status, state)


def map_episode_state(ep: _ArrLike) -> str:
    """Map a Sonarr episode entry to ready, downloading, queued or searching.

    NZBGet transfers one NZB at a time, so paused, queued or delayed items with partial
    or zero progress are labelled queued; calling them downloading would overstate
    progress. One that failed, that no download client holds, or that a warning keeps from
    starting is bringing nothing, whatever the sizes it reports.
    """
    progress_raw = ep.get("progress", 0)
    progress = progress_raw if isinstance(progress_raw, int) else 0
    sizeleft_raw = ep.get("sizeleft", 0)
    sizeleft = sizeleft_raw if isinstance(sizeleft_raw, int) else 0
    size_raw = ep.get("size", 0)
    size = size_raw if isinstance(size_raw, int) else 0
    status_raw = ep.get("status") or ""
    status = status_raw.lower() if isinstance(status_raw, str) else ""

    if status in ("failed", "downloadclientunavailable"):
        return "searching"
    # A completed entry may report no size at all.
    if status == "completed" or progress == 100 or (sizeleft == 0 and size > 0):
        return "ready"
    if status == "downloading":
        return "downloading"
    if status in ("paused", "queued", "delay") or progress > 0:
        return "queued"
    return "searching"
