"""Classification and state-mapping helpers for the downloads page."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime

from mediaman.core.format import format_day_month
from mediaman.core.time import now_utc, parse_iso_utc

# rationale: these helpers consume *arr response shapes that are described
# by TypedDicts in :mod:`mediaman.services.arr._types`, but mypy treats a
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

logger = logging.getLogger(__name__)


def extract_poster_url(images: Sequence[_ArrLike] | None) -> str:
    """Return the first poster remoteUrl from an *arr images list, or ``""``.

    Iterates the ``images`` list (as returned by Radarr/Sonarr API responses)
    and returns the ``remoteUrl`` of the first entry whose ``coverType`` is
    ``"poster"``. Returns ``""`` when no matching entry is found or when
    ``images`` is falsy — callers no longer need ``or ""`` patches.
    """
    for img in images or []:
        remote_url = img.get("remoteUrl")
        if img.get("coverType") == "poster" and isinstance(remote_url, str) and remote_url:
            return remote_url
    return ""


def _format_release_date(dt: datetime) -> str:
    """Format a datetime as '<d MMM yyyy>' e.g. '14 Jun 2099'.

    Uses :func:`~mediaman.services.infra.format.format_day_month` rather than
    ``%-d`` (a GNU-only strftime extension that fails on Windows/BSD).
    """
    return format_day_month(dt)


def compute_movie_released_at(movie: _ArrLike) -> float:
    """Return the earliest known release-date epoch for a Radarr movie, or 0.0.

    Looks at ``digitalRelease``, ``physicalRelease`` and ``inCinemas`` and
    returns the earliest parseable timestamp as POSIX seconds. Future-dated
    entries are accepted (callers can compare against ``now`` themselves);
    only the year-9999 sentinel and other absurdly far-future values
    (beyond :data:`_MAX_FUTURE_YEARS`) are filtered out.

    Returns ``0.0`` when none of the fields are populated or parseable —
    auto-abandon treats that as "release date unknown" and skips the item
    rather than guessing.
    """
    now = now_utc()
    max_year = now.year + _MAX_FUTURE_YEARS
    candidates: list[datetime] = []
    for key in ("digitalRelease", "physicalRelease", "inCinemas"):
        raw = movie.get(key, "")
        dt = parse_iso_utc(raw if isinstance(raw, str) else "")
        if dt and dt.year <= max_year:
            candidates.append(dt)
    if not candidates:
        return 0.0
    return min(candidates).timestamp()


def compute_series_released_at(episodes: Sequence[_ArrLike]) -> float:
    """Return the most recent past airing epoch across *episodes*, or 0.0.

    Used by auto-abandon to gate "too fresh to abandon" decisions. The
    *latest* past airing matters here, not the earliest: a long-running
    series whose first episode aired in 2010 may have a missing episode
    that aired last week — abandoning that episode early would bin a
    legitimate search just because the series itself is old.

    Future airings are ignored because :func:`classify_series_upcoming`
    already covers the upcoming case via ``is_upcoming``. Returns ``0.0``
    when no episode has a parseable past ``airDateUtc``.
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


def classify_movie_upcoming(movie: _ArrLike) -> tuple[bool, str]:
    """Classify a Radarr movie as upcoming and build its release label.

    Returns (is_upcoming, release_label). Label is "" when not upcoming.
    """
    if not movie.get("monitored"):
        return False, ""
    if movie.get("hasFile"):
        return False, ""
    if movie.get("isAvailable"):
        return False, ""

    now = now_utc()
    max_year = now.year + _MAX_FUTURE_YEARS
    candidates = []
    for key in ("digitalRelease", "physicalRelease", "inCinemas"):
        raw = movie.get(key, "")
        dt = parse_iso_utc(raw if isinstance(raw, str) else "")
        if dt and dt > now and dt.year <= max_year:
            candidates.append(dt)

    if candidates:
        earliest = min(candidates)
        return True, f"Releases {_format_release_date(earliest)}"
    return True, "Not yet released"


def classify_series_upcoming(series: _ArrLike, episodes: Sequence[_ArrLike]) -> tuple[bool, str]:
    """Classify a Sonarr series as upcoming and build its premiere label.

    Returns ``(is_upcoming, release_label)``. Label is ``""`` when not upcoming.

    ``episodes`` is the list of episodes for this series (may be empty).
    An empty list is treated as "no aired episodes".

    Episodes whose ``airDateUtc`` field is missing or cannot be parsed are
    counted and logged at DEBUG level, then placed in an "unknown airdate"
    bucket.  They do not affect the classification but are not silently
    dropped -- the log entry shows the count so operators can investigate.
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

    return False, ""


def map_state(nzbget_status: str | None, has_nzbget_match: bool) -> str:
    """Map internal NZBGet/Arr state to a user-facing state string.

    Returns one of: "searching", "downloading", "almost_ready".
    """
    if not has_nzbget_match or nzbget_status is None:
        return "searching"
    upper = nzbget_status.upper()
    if "UNPACKING" in upper or "PP_" in upper:
        return "almost_ready"
    if "DOWNLOADING" in upper or "PAUSED" in upper:
        return "downloading"
    return "searching"


def map_arr_status(status: str, tracked_state: str = "") -> str:
    """Map a Radarr/Sonarr queue item to a user-facing state.

    Checks both the queue ``status`` field and ``trackedDownloadState``
    since different Radarr/Sonarr versions populate these differently.

    Returns one of: ``"downloading"``, ``"almost_ready"``, ``"searching"``.
    """
    lower = (status or "").lower()
    tracked = (tracked_state or "").lower()

    if tracked in ("importing", "importpending", "imported"):
        return "almost_ready"
    if tracked == "downloading":
        return "downloading"

    if lower == "downloading":
        return "downloading"
    if lower == "completed":
        return "almost_ready"

    if lower in ("queued", "paused", "delay"):
        return "downloading"

    return "searching"


def map_episode_state(ep: _ArrLike) -> str:
    """Map a Sonarr episode entry to a simplified state.

    Returns one of: "ready", "downloading", "queued", "searching".

    NZBGet transfers one NZB at a time by default. Items sitting in the
    queue with partial or zero progress show up in Sonarr as ``paused``,
    ``queued`` or ``delay`` — calling those "downloading" overstates
    what's actually happening, so they get a distinct "queued" label.
    """
    progress_raw = ep.get("progress", 0)
    progress = progress_raw if isinstance(progress_raw, int) else 0
    sizeleft_raw = ep.get("sizeleft", 0)
    sizeleft = sizeleft_raw if isinstance(sizeleft_raw, int) else 0
    size_raw = ep.get("size", 0)
    size = size_raw if isinstance(size_raw, int) else 0
    status_raw = ep.get("status") or ""
    status = status_raw.lower() if isinstance(status_raw, str) else ""

    if progress == 100 or (sizeleft == 0 and size > 0):
        return "ready"
    if status == "downloading":
        return "downloading"
    if status in ("paused", "queued", "delay", "warning") or progress > 0:
        return "queued"
    return "searching"
