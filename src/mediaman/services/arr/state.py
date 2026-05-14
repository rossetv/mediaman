"""Compute Radarr/Sonarr download state for a media item.

States:
- ``in_library`` — movie has a file, OR every aired season of the TV
  show has every episode downloaded.
- ``partial`` — TV only; at least one aired season has files but not
  all aired episodes are present.
- ``downloading`` — item is in the Arr download queue.
- ``queued`` — item is added to Radarr/Sonarr but has no files yet
  and is not in the queue.
- ``None`` — item is not tracked at all.

``ACTION_*`` constants are the canonical strings used as the ``action``
column value in ``compute_download_state``.  Import these instead of
repeating the string literals so a future rename touches one place.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Final, TypedDict

from mediaman.services.arr._types import (
    ArrSeason,
    ArrSeasonStatistics,
    RadarrMovie,
    SonarrSeries,
)

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Download-state action constants
# ---------------------------------------------------------------------------

#: Item has every file present.
ACTION_IN_LIBRARY: Final = "in_library"
#: TV item has *some* episode files but not all aired episodes.
ACTION_PARTIAL: Final = "partial"
#: Item is currently in the Arr download queue.
ACTION_DOWNLOADING: Final = "downloading"
#: Item is tracked by Arr but has no files and is not in the queue.
ACTION_QUEUED: Final = "queued"


class RadarrCaches(TypedDict):
    """Cache fragment returned by :func:`build_radarr_cache`."""

    radarr_movies: dict[int, RadarrMovie]
    radarr_queue_tmdb_ids: set[int]


class SonarrCaches(TypedDict):
    """Cache fragment returned by :func:`build_sonarr_cache`."""

    sonarr_series: dict[int, SonarrSeries]
    sonarr_queue_tmdb_ids: set[int]


class ArrCaches(RadarrCaches, SonarrCaches):
    """Full merged cache; produced by ``{**build_radarr_cache(), **build_sonarr_cache()}``."""


def series_has_files(series_data: SonarrSeries) -> bool:
    """Return True if Sonarr reports at least one episode file is present."""
    return (series_data.get("statistics") or {}).get("episodeFileCount", 0) > 0


def _season_stats(season: ArrSeason) -> ArrSeasonStatistics:
    """Return a season's ``statistics`` dict, or an empty one if absent/malformed."""
    stats = season.get("statistics")
    return stats if isinstance(stats, dict) else ArrSeasonStatistics()


def _season_has_aired(season: ArrSeason) -> bool:
    """Return True if Sonarr signals at least one episode of *season* has aired.

    Sonarr v3 exposes this as ``previousAiring`` on the season's
    statistics.  Older Sonarr versions used ``previousAiringDate`` on
    the season payload itself; we accept either so a freshly-upgraded
    Sonarr (or a downgrade) doesn't silently report every season as
    unaired.
    """
    stats = _season_stats(season)
    if stats.get("previousAiring"):
        return True
    return bool(season.get("previousAiringDate") or stats.get("previousAiringDate"))


def _compute_series_state(series: SonarrSeries, tmdb_id: int, caches: ArrCaches) -> str | None:
    """Return the download state for a tracked Sonarr *series*.

    Extracted verbatim from the series branch of
    :func:`compute_download_state`; *series* is the already-resolved
    cache entry (never ``None``).
    """
    # Only consider seasons that have aired and are not season 0 (specials).
    # We additionally require ``monitored=True`` so an unmonitored season
    # the user explicitly skipped doesn't drag the show into ``partial``.
    aired_seasons = [
        s
        for s in series.get("seasons", [])
        if s.get("seasonNumber", 0) > 0 and s.get("monitored", True) and _season_has_aired(s)
    ]

    if aired_seasons:
        # ``have_all`` requires ``episodeCount > 0`` per season on purpose:
        # right after Sonarr flips a brand-new season to ``aired``, it
        # briefly reports ``episodeCount == 0`` and ``episodeFileCount ==
        # 0``.  Without the ``> 0`` guard the boolean ``0 >= 0`` would
        # silently satisfy ``have_all`` and a fully-downloaded show with
        # one not-yet-populated season would flip to ``in_library`` for
        # one polling cycle.  Treating the season as ``partial`` for a
        # cycle is the lesser evil — see ``test_arr_state.py:
        # test_tv_aired_season_with_zero_episode_count_does_not_mask_partial``
        # for the regression that pinned this behaviour.
        have_any = any(_season_stats(s).get("episodeFileCount", 0) > 0 for s in aired_seasons)
        have_all = all(
            _season_stats(s).get("episodeFileCount", 0) >= _season_stats(s).get("episodeCount", 0)
            and _season_stats(s).get("episodeCount", 0) > 0
            for s in aired_seasons
        )
        if have_all:
            return ACTION_IN_LIBRARY
        if have_any:
            return ACTION_PARTIAL

    if tmdb_id in caches["sonarr_queue_tmdb_ids"]:
        return ACTION_DOWNLOADING
    return ACTION_QUEUED


def compute_download_state(media_type: str, tmdb_id: int, caches: ArrCaches) -> str | None:
    """Return the download state for an item, or ``None`` if untracked.

    Args:
        media_type: Either ``"movie"`` or ``"tv"``.
        tmdb_id: The TMDB identifier for the item.
        caches: Pre-fetched Radarr/Sonarr data keyed by TMDB ID.

    Returns:
        One of ``"in_library"``, ``"partial"``, ``"downloading"``,
        ``"queued"``, or ``None``.
    """
    if media_type == "movie":
        movie = caches["radarr_movies"].get(tmdb_id)
        if movie is None:
            return None
        if movie.get("hasFile"):
            return ACTION_IN_LIBRARY
        if tmdb_id in caches["radarr_queue_tmdb_ids"]:
            return ACTION_DOWNLOADING
        # An unmonitored Radarr entry is the residue of a previous abandon
        # (manual or auto). Reporting it as ``queued`` would surface a
        # disabled "Queued" button that wedges the user — they can't
        # re-download a movie they abandoned. Treat it as untracked here
        # and let the search/download endpoint re-monitor on click.
        if not movie.get("monitored", True):
            return None
        return ACTION_QUEUED

    series = caches["sonarr_series"].get(tmdb_id)
    if series is None:
        return None
    return _compute_series_state(series, tmdb_id, caches)


def build_radarr_cache(client: ArrClient | None) -> RadarrCaches:
    """Build the per-request Radarr cache fragment. Returns a partial
    ``ArrCaches`` containing only the Radarr keys; combine with
    ``build_sonarr_cache`` via dict-spread to get a full ``ArrCaches``.
    ``client`` may be ``None``.

    Two movies in the Radarr library can in principle share a ``tmdbId``
    (typically because the operator added a release through manual TMDB
    lookup for a duplicate entry). The dict comprehension would silently
    keep only the second one, hiding the duplicate. We log a warning so
    the operator can clean up the library — taking the last entry is
    deliberate (matches dict-update semantics) and stable across calls.
    """
    if client is None:
        return {"radarr_movies": {}, "radarr_queue_tmdb_ids": set()}
    movies: dict[int, RadarrMovie] = {}
    for m in client.get_movies():
        tid = m.get("tmdbId")
        if not tid:
            continue
        if tid in movies:
            existing_title = movies[tid].get("title")
            new_title = m.get("title")
            logger.warning(
                "build_radarr_cache: duplicate tmdbId=%s in Radarr library "
                "(existing=%r, new=%r) — keeping the later entry; "
                "disambiguation by tmdb_id may be unreliable for this title",
                tid,
                existing_title,
                new_title,
            )
        movies[tid] = m
    queue_ids: set[int] = {
        tid for q in client.get_queue() if (tid := (q.get("movie") or {}).get("tmdbId"))
    }
    return {"radarr_movies": movies, "radarr_queue_tmdb_ids": queue_ids}


def build_sonarr_cache(client: ArrClient | None) -> SonarrCaches:
    """Build the per-request Sonarr cache fragment. Returns a partial
    ``ArrCaches`` containing only the Sonarr keys; combine with
    ``build_radarr_cache`` via dict-spread to get a full ``ArrCaches``.
    ``client`` may be ``None``.

    See :func:`build_radarr_cache` for the duplicate-tmdb-id handling.
    """
    if client is None:
        return {"sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}
    series: dict[int, SonarrSeries] = {}
    for s in client.get_series():
        tid = s.get("tmdbId")
        if not tid:
            continue
        if tid in series:
            existing_title = series[tid].get("title")
            new_title = s.get("title")
            logger.warning(
                "build_sonarr_cache: duplicate tmdbId=%s in Sonarr library "
                "(existing=%r, new=%r) — keeping the later entry; "
                "disambiguation by tmdb_id may be unreliable for this title",
                tid,
                existing_title,
                new_title,
            )
        series[tid] = s
    queue_ids: set[int] = {
        tid for q in client.get_queue() if (tid := (q.get("series") or {}).get("tmdbId"))
    }
    return {"sonarr_series": series, "sonarr_queue_tmdb_ids": queue_ids}


# ---------------------------------------------------------------------------
# LazyArrClients — request-scoped Radarr/Sonarr client pair
# ---------------------------------------------------------------------------


class LazyArrClients:
    """Request-scoped Radarr/Sonarr client pair built lazily from DB settings.

    Each client is built at most once per :class:`LazyArrClients` instance.
    Call :meth:`radarr` / :meth:`sonarr` to obtain the client (or ``None``
    when the service is not configured).

    Args:
        conn: Open SQLite connection with ``row_factory`` set to
            :class:`sqlite3.Row`.
        secret_key: Application secret used to decrypt encrypted settings.
    """

    def __init__(self, conn: sqlite3.Connection, secret_key: str) -> None:
        self._conn = conn
        self._secret_key = secret_key
        self._radarr: ArrClient | None = None
        self._radarr_built: bool = False
        self._sonarr: ArrClient | None = None
        self._sonarr_built: bool = False

    def radarr(self) -> ArrClient | None:
        """Return the Radarr :class:`~mediaman.services.arr.base.ArrClient`, building it on first call."""
        if not self._radarr_built:
            from mediaman.services.arr.build import build_radarr_from_db

            self._radarr = build_radarr_from_db(self._conn, self._secret_key)
            self._radarr_built = True
        return self._radarr

    def sonarr(self) -> ArrClient | None:
        """Return the Sonarr :class:`~mediaman.services.arr.base.ArrClient`, building it on first call."""
        if not self._sonarr_built:
            from mediaman.services.arr.build import build_sonarr_from_db

            self._sonarr = build_sonarr_from_db(self._conn, self._secret_key)
            self._sonarr_built = True
        return self._sonarr


def attach_download_states(
    batches: list[dict[str, object]], arr: LazyArrClients
) -> dict[object, dict[str, object]]:
    """Annotate each recommendation with its Arr download state, in place.

    Walks every ``trending``/``personal`` item across *batches*; for an
    item with a ``tmdb_id`` it computes :func:`compute_download_state`
    against the relevant Arr cache and, when that yields a non-``None``
    state, writes it onto ``item["download_state"]``.

    The Radarr and Sonarr caches are built lazily — at most once each, and
    only if a movie / TV item is actually present — via *arr*. The two
    *empty* cache halves are built once up front rather than per iteration
    (the loop previously rebuilt the unused half on every opposite-branch
    item, which was needless repeated work).

    Returns a ``{item["id"]: item}`` map of every item seen, so the caller
    can serialise the full set without re-walking the batches.
    """
    empty_radarr = build_radarr_cache(None)
    empty_sonarr = build_sonarr_cache(None)

    radarr_cache: RadarrCaches | None = None
    sonarr_cache: SonarrCaches | None = None

    all_recs: dict[object, dict[str, object]] = {}
    for batch in batches:
        trending = batch["trending"]
        personal = batch["personal"]
        if not isinstance(trending, list) or not isinstance(personal, list):
            continue
        for item in trending + personal:
            tmdb_id = item.get("tmdb_id")
            media_type = item.get("media_type")
            if tmdb_id and isinstance(tmdb_id, int) and isinstance(media_type, str):
                if media_type == "movie":
                    if radarr_cache is None:
                        radarr_cache = build_radarr_cache(arr.radarr())
                    caches: ArrCaches = {**radarr_cache, **empty_sonarr}
                else:
                    if sonarr_cache is None:
                        sonarr_cache = build_sonarr_cache(arr.sonarr())
                    caches = {**empty_radarr, **sonarr_cache}
                state = compute_download_state(media_type, tmdb_id, caches)
                if state is not None:
                    item["download_state"] = state

            all_recs[item["id"]] = item
    return all_recs
