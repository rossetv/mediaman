# rationale: the pieces
# (state constants, state computation, cache builders, LazyArrClients, and the
# attach_download_states orchestrator) are tightly coupled around the ArrCaches
# shape, and attach_download_states cannot move to web/repository without
# inverting the dependency graph (the arr package sits below web). Splitting now
# would scatter one cohesive concern across files with no navigability win.
"""Compute Radarr/Sonarr download state for a media item.

States:
- ``in_library`` — movie has a file, OR every aired season of the TV
  show has every episode downloaded.
- ``partial`` — TV only; at least one aired season has files but not
  all aired episodes are present.
- ``downloading`` — item is in the Arr download queue.
- ``queued`` — item is added to Radarr/Sonarr but has no files yet, is
  not in the queue and is set to be fetched (an unmonitored one is not).
- ``None`` — item is not tracked at all.

``ACTION_*`` constants are the canonical strings used as the ``action``
column value in ``compute_download_state``.  Import these instead of
repeating the string literals so a future rename touches one place.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal, NotRequired, TypedDict

import requests

from mediaman.services.arr._transport import ArrError
from mediaman.services.arr._types import (
    ArrSeason,
    ArrSeasonStatistics,
    RadarrMovie,
    SonarrSeries,
)
from mediaman.services.infra import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)

#: Item has every file present.
ACTION_IN_LIBRARY: Final = "in_library"
#: TV item has *some* episode files but not all aired episodes.
ACTION_PARTIAL: Final = "partial"
#: Item is currently in the Arr download queue.
ACTION_DOWNLOADING: Final = "downloading"
#: Item is tracked by Arr but has no files and is not in the queue.
ACTION_QUEUED: Final = "queued"


class RadarrCaches(TypedDict):
    radarr_movies: dict[int, RadarrMovie]
    radarr_queue_tmdb_ids: set[int]
    #: tmdbId -> download percentage 0-100; absent in hand-built caches.
    radarr_queue_progress: NotRequired[dict[int, int]]


class SonarrCaches(TypedDict):
    sonarr_series: dict[int, SonarrSeries]
    sonarr_queue_tmdb_ids: set[int]
    sonarr_queue_progress: NotRequired[dict[int, int]]


class ArrCaches(RadarrCaches, SonarrCaches):
    pass


def series_has_files(series_data: SonarrSeries) -> bool:
    """Return True if Sonarr reports at least one episode file is present.

    Assumes ``absent == 0`` for ``episodeFileCount``: the ``_types``
    TypedDicts are ``total=False`` and *arr omits *only* zero-valued integer
    counts (never a populated one), so a missing field is safely read as 0.
    Do not "fix" the ``.get(..., 0)`` into a strict subscript — that would
    KeyError on the legitimate zero-count case.
    """
    return (series_data.get("statistics") or {}).get("episodeFileCount", 0) > 0


def _season_stats(season: ArrSeason) -> ArrSeasonStatistics:
    """Return a season's ``statistics`` dict, or an empty one if absent/malformed.

    Assumes ``absent == 0`` for every count read off the returned dict
    (``episodeFileCount`` / ``episodeCount``): *arr omits only zero-valued
    counts, so the ``.get(..., 0)`` reads at the call sites are correct.
    """
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

    *series* is the already-resolved cache entry (never ``None``).
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
    return ACTION_QUEUED if _sonarr_will_fetch(series) else None


def _sonarr_will_fetch(series: SonarrSeries) -> bool:
    """Whether Sonarr, as set, is going to fetch something for *series*.

    Sonarr picks episodes, not seasons: a season left unmonitored can still
    hold single monitored episodes. So in any season, a monitored episode
    without a file (``episodeCount`` above ``episodeFileCount``) or one still
    to air (``nextAiring``) is something to fetch. An unmonitored series, or
    one with nothing of either kind, will never be fetched, so "queued" would
    be false. A monitored season that does not show itself complete, and a
    series that lists no seasons, stay queued.
    """
    if series.get("monitored") is False:
        return False
    seasons = series.get("seasons")
    if not isinstance(seasons, list) or not seasons:
        return True
    for season in seasons:
        stats = _season_stats(season)
        total = stats.get("episodeCount", 0)
        files = stats.get("episodeFileCount", 0)
        if total > files or stats.get("nextAiring"):
            return True
        if season.get("monitored", True) is not False and not (total > 0 and files >= total):
            return True
    return False


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


def _build_arr_index(
    client: ArrClient,
    *,
    items_getter: str,
    queue_tmdb_key: str,
    cache_name: str,
) -> dict[int, RadarrMovie] | dict[int, SonarrSeries]:
    """Fetch all library items and build a ``{tmdbId: item}`` index.

    Shared implementation for ``build_radarr_cache`` and
    ``build_sonarr_cache``, which were ~90% identical; the
    ``_ArrLibraryIndex`` helper in ``completion/_verification.py`` builds the
    same tmdbId-keyed dict.

    Args:
        client: Configured ``ArrClient`` (never ``None`` — callers guard).
        items_getter: Name of the method to call to fetch all items
            (``"get_movies"`` or ``"get_series"``).
        queue_tmdb_key: Key under each queue entry's media sub-dict that holds
            the tmdbId (``"movie"`` for Radarr, ``"series"`` for Sonarr).
        cache_name: Human-readable name for warning messages (``"build_radarr_cache"``
            or ``"build_sonarr_cache"``).

    Warns on duplicate ``tmdbId`` values in the library — takes the later entry,
    matching dict-update semantics.
    """
    index: dict[int, RadarrMovie] | dict[int, SonarrSeries] = {}
    for item in getattr(client, items_getter)():
        tid = item.get("tmdbId")
        if not tid:
            continue
        if tid in index:
            logger.warning(
                "%s: duplicate tmdbId=%s in library "
                "(existing=%r, new=%r) — keeping the later entry; "
                "disambiguation by tmdb_id may be unreliable for this title",
                cache_name,
                tid,
                index[tid].get("title"),
                item.get("title"),
            )
        index[tid] = item
    return index


def _queue_progress(queue: Sequence[Mapping[str, Any]], media_key: str) -> dict[int, int]:
    """Return ``{tmdbId: pct}`` for queue entries, summing ``size``/``sizeleft`` per item
    so a series with several queued episodes reports one overall percentage. An entry the
    downloads page shows as still searched for is left out.
    """
    from mediaman.services.arr.fetcher._base import clamp_progress
    from mediaman.services.downloads.download_format import map_episode_state

    totals: dict[int, list[int]] = {}
    for q in queue:
        tid = (q.get(media_key) or {}).get("tmdbId")
        if not tid:
            continue
        size, left = q.get("size") or 0, q.get("sizeleft") or 0
        status = q.get("status") or q.get("trackedDownloadStatus") or "queued"
        entry = {"size": size, "sizeleft": left, "progress": clamp_progress(size, left)}
        if map_episode_state({**entry, "status": status}) == "searching":
            continue
        acc = totals.setdefault(tid, [0, 0])
        acc[0] += size
        acc[1] += left
    return {tid: clamp_progress(size, left) for tid, (size, left) in totals.items()}


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
        return {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "radarr_queue_progress": {},
        }
    movies = _build_arr_index(
        client,
        items_getter="get_movies",
        queue_tmdb_key="movie",
        cache_name="build_radarr_cache",
    )
    queue = client.get_queue()
    progress = _queue_progress(queue, "movie")
    return {
        "radarr_movies": movies,  # type: ignore[typeddict-item]
        "radarr_queue_tmdb_ids": set(progress),
        "radarr_queue_progress": progress,
    }


def build_sonarr_cache(client: ArrClient | None) -> SonarrCaches:
    """Build the per-request Sonarr cache fragment. Returns a partial
    ``ArrCaches`` containing only the Sonarr keys; combine with
    ``build_radarr_cache`` via dict-spread to get a full ``ArrCaches``.
    ``client`` may be ``None``.

    See ``build_radarr_cache`` for the duplicate-tmdb-id handling.
    """
    if client is None:
        return {
            "sonarr_series": {},
            "sonarr_queue_tmdb_ids": set(),
            "sonarr_queue_progress": {},
        }
    series = _build_arr_index(
        client,
        items_getter="get_series",
        queue_tmdb_key="series",
        cache_name="build_sonarr_cache",
    )
    queue = client.get_queue()
    progress = _queue_progress(queue, "series")
    return {
        "sonarr_series": series,  # type: ignore[typeddict-item]
        "sonarr_queue_tmdb_ids": set(progress),
        "sonarr_queue_progress": progress,
    }


def annotate_download_states(
    results: list[dict[str, object]],
    conn: sqlite3.Connection,
    secret_key: str,
) -> None:
    """Stamp ``download_state`` onto each result dict in *results*, in place; queued and
    downloading items also get ``download_pct`` (0-100, 0 when not yet in the queue).

    Builds the Radarr and Sonarr caches from DB settings, then walks
    *results* and calls ``compute_download_state`` for each item that
    has a ``tmdb_id`` and ``media_type``.  Failures to build either cache
    are logged and treated as empty (the item receives ``download_state=None``).

    This is a service function — route handlers call it instead of
    inlining the cache-build + iteration loop.

    Args:
        results: List of normalised TMDB result dicts to annotate in place.
        conn:  Open SQLite connection.
        secret_key: Application secret for decrypting Arr settings.
    """
    import requests as _requests

    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
    from mediaman.services.infra import SafeHTTPError

    try:
        radarr_cache = build_radarr_cache(build_radarr_from_db(conn, secret_key))
    except (_requests.RequestException, SafeHTTPError, sqlite3.Error):
        logger.warning(
            "Radarr cache build failed; search results will not reflect Radarr state",
            exc_info=True,
        )
        radarr_cache = build_radarr_cache(None)

    try:
        sonarr_cache = build_sonarr_cache(build_sonarr_from_db(conn, secret_key))
    except (_requests.RequestException, SafeHTTPError, sqlite3.Error):
        logger.warning(
            "Sonarr cache build failed; search results will not reflect Sonarr state",
            exc_info=True,
        )
        sonarr_cache = build_sonarr_cache(None)

    caches: ArrCaches = {**radarr_cache, **sonarr_cache}
    for r in results:
        tmdb_id = r.get("tmdb_id")
        if tmdb_id and isinstance(tmdb_id, int):
            media_type = r["media_type"]
            state = compute_download_state(str(media_type), tmdb_id, caches)
            r["download_state"] = state
            if state in (ACTION_QUEUED, ACTION_DOWNLOADING):
                progress = (
                    caches.get("radarr_queue_progress", {})
                    if media_type == "movie"
                    else caches.get("sonarr_queue_progress", {})
                )
                r["download_pct"] = progress.get(tmdb_id, 0)


def is_series_already_tracked(sonarr_client: ArrClient, tvdb_id: int) -> bool:
    """Return True if Sonarr already tracks the series with *tvdb_id*.

    Iterates the full series list returned by Sonarr and checks for a
    matching ``tvdbId``.  This is a service-layer function so route handlers
    are not responsible for iterating external-service payloads.

    Args:
        sonarr_client: A configured Sonarr ``ArrClient``.
        tvdb_id: The TVDB ID to look up.

    Raises:
        SafeHTTPError: On a non-2xx response from Sonarr.
        requests.RequestException: On transport failures.

    Returns:
        ``True`` when the series is already tracked; ``False`` otherwise.
    """
    return any(s.get("tvdbId") == tvdb_id for s in sonarr_client.get_series())


def radarr_holds_movie(radarr_client: ArrClient, tmdb_id: int) -> bool:
    """Whether Radarr holds *tmdb_id*; an unreadable answer counts as no.

    For telling a refused add (Radarr answers 400, not 409, for a movie it
    holds) from one that failed for another reason.
    """
    try:
        return radarr_client.get_movie_by_tmdb(tmdb_id) is not None
    except (SafeHTTPError, requests.RequestException, ArrError):
        return False


def sonarr_tracks_series(sonarr_client: ArrClient, tvdb_id: int) -> bool:
    """Whether Sonarr tracks *tvdb_id*; an unreadable answer counts as no."""
    try:
        return is_series_already_tracked(sonarr_client, tvdb_id)
    except (SafeHTTPError, requests.RequestException, ArrError):
        return False


def radarr_fetches_movie(radarr_client: ArrClient, tmdb_id: int) -> bool:
    """Whether Radarr holds *tmdb_id* and will have it: monitored, or already with a file."""
    try:
        movie = radarr_client.get_movie_by_tmdb(tmdb_id)
    except (SafeHTTPError, requests.RequestException, ArrError):
        return False
    return movie is not None and bool(movie.get("monitored") or movie.get("hasFile"))


def sonarr_fetches_series(sonarr_client: ArrClient, tvdb_id: int) -> bool:
    """Whether Sonarr tracks *tvdb_id* and is going to fetch something for it."""
    try:
        series = next((s for s in sonarr_client.get_series() if s.get("tvdbId") == tvdb_id), None)
    except (SafeHTTPError, requests.RequestException, ArrError):
        return False
    return series is not None and _sonarr_will_fetch(series)


MovieAddOutcome = Literal["added", "remonitored", "already_tracked", "bad_entry"]


class ExistingMovieError(ArrError):
    """Radarr refused to be read or re-monitored for a movie it holds.

    Kept apart from a failed add: Radarr's 409, 422 and 400 on an add mean
    "already there", but on these calls they mean nothing of the kind.
    """


def add_or_remonitor_movie(
    radarr_client: ArrClient, tmdb_id: int, title: str, *, keep_filed: bool = False
) -> MovieAddOutcome:
    """Get a movie wanted in Radarr: add it, or re-monitor the entry it already holds.

    A monitored entry is genuinely tracked (``"already_tracked"``), so a
    double-click does not churn it. An unmonitored one is the residue of an
    abandon: re-monitoring it (which also searches) is what pressing Download
    again means. An entry with no usable id is ``"bad_entry"``. With
    *keep_filed* an entry that already has its file is left alone too.

    Raises:
        SafeHTTPError: On a non-2xx response to the add only.
        ExistingMovieError: On a non-2xx response reading or re-monitoring the entry.
        requests.RequestException: On transport failures.
        ArrError: On configuration failures.
    """
    try:
        existing = radarr_client.get_movie_by_tmdb(tmdb_id)
    except SafeHTTPError as exc:
        raise ExistingMovieError(f"Radarr lookup failed: HTTP {exc.status_code}") from exc
    if existing is None:
        radarr_client.add_movie(tmdb_id, title)
        return "added"
    if existing.get("monitored") or (keep_filed and existing.get("hasFile")):
        return "already_tracked"
    movie_id = existing.get("id")
    if not isinstance(movie_id, int) or movie_id <= 0:
        logger.warning(
            "unmonitored radarr entry has bad id=%r for tmdb=%s",
            movie_id,
            tmdb_id,
        )
        return "bad_entry"
    try:
        radarr_client.remonitor_movie(movie_id)
    except (SafeHTTPError, requests.RequestException, ArrError) as exc:
        # The monitor may have landed before the search failed. Left monitored the
        # movie would read as already tracked, and the same press could never search it.
        _put_back_unmonitored(radarr_client, movie_id)
        if isinstance(exc, SafeHTTPError):
            raise ExistingMovieError(f"Radarr re-monitor failed: HTTP {exc.status_code}") from exc
        raise
    return "remonitored"


def _put_back_unmonitored(radarr_client: ArrClient, movie_id: int) -> None:
    """Best effort: a failure here is logged, so the error being raised is not hidden."""
    try:
        radarr_client.unmonitor_movie(movie_id, max_retries=1)
    except (SafeHTTPError, requests.RequestException, ValueError, ArrError):
        logger.exception("Could not put radarr movie_id=%s back to unmonitored", movie_id)


class LazyArrClients:
    """Request-scoped Radarr/Sonarr client pair built lazily from DB settings.

    Each client is built at most once per ``LazyArrClients`` instance.
    Call ``radarr`` / ``sonarr`` to obtain the client (or ``None``
    when the service is not configured).

    Args:
        conn: Open SQLite connection with ``row_factory`` set to
            ``sqlite3.Row``.
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
        if not self._radarr_built:
            from mediaman.services.arr.build import build_radarr_from_db

            self._radarr = build_radarr_from_db(self._conn, self._secret_key)
            self._radarr_built = True
        return self._radarr

    def sonarr(self) -> ArrClient | None:
        if not self._sonarr_built:
            from mediaman.services.arr.build import build_sonarr_from_db

            self._sonarr = build_sonarr_from_db(self._conn, self._secret_key)
            self._sonarr_built = True
        return self._sonarr


def _build_radarr_cache_resilient(client: ArrClient | None) -> tuple[RadarrCaches, bool]:
    """Build the Radarr cache, degrading to empty if Radarr is unreachable.

    Returns ``(cache, configured)``. On an expected upstream failure —
    ``SafeHTTPError`` (transport) or ``ArrError`` and subclasses (domain) — we
    log a WARNING and return an empty cache with ``configured=False`` so the
    recommendations page renders without Radarr badges instead of 500ing, and a
    down Radarr never wipes a freshly-optimistic ``downloaded_at`` flag.
    Programming errors (``KeyError``/``TypeError``/…) are not caught.
    """
    from mediaman.services.arr import ArrError
    from mediaman.services.infra import SafeHTTPError

    try:
        return build_radarr_cache(client), client is not None
    except (SafeHTTPError, ArrError):
        logger.warning(
            "attach_download_states: Radarr unavailable; recommendations will "
            "render without Radarr download state",
            exc_info=True,
        )
        return build_radarr_cache(None), False


def _build_sonarr_cache_resilient(client: ArrClient | None) -> tuple[SonarrCaches, bool]:
    """Build the Sonarr cache, degrading to empty if Sonarr is unreachable.

    Mirror of ``_build_radarr_cache_resilient`` for Sonarr. Sonarr fails
    independently of Radarr: a down Sonarr must not hide Radarr state.
    """
    from mediaman.services.arr import ArrError
    from mediaman.services.infra import SafeHTTPError

    try:
        return build_sonarr_cache(client), client is not None
    except (SafeHTTPError, ArrError):
        logger.warning(
            "attach_download_states: Sonarr unavailable; recommendations will "
            "render without Sonarr download state",
            exc_info=True,
        )
        return build_sonarr_cache(None), False


def clear_stale_downloaded_at(conn: sqlite3.Connection, ids: Sequence[object]) -> None:
    """Clear ``downloaded_at`` on the suggestions *ids* that Radarr/Sonarr no longer track.

    Runs in one transaction so a locked database rolls back cleanly on a render path.
    """
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with conn:
        # rationale: placeholders is purely "?" * len(ids) — no user value ever enters the SQL text
        conn.execute(  # nosec B608
            f"UPDATE suggestions SET downloaded_at = NULL WHERE id IN ({placeholders})",
            list(ids),
        )


# NOTE: this module hosts several concepts — state constants, state
# computation, cache builders, LazyArrClients, and this recommendation-
# annotation orchestrator. ``attach_download_states`` arguably belongs nearer
# the recommendations service, but its ``suggestions``-table UPDATE cannot move
# into the ``mediaman.web.repository`` layer (the arr package sits below web in
# the dependency graph and must not import upward). Left here for that reason.
def attach_download_states(
    batches: list[dict[str, object]],
    arr: LazyArrClients,
    conn: sqlite3.Connection | None = None,
) -> dict[object, dict[str, object]]:
    """Annotate each recommendation with its Arr download state, in place.

    Walks every ``trending``/``personal`` item across *batches*; for an
    item with a ``tmdb_id`` it computes ``compute_download_state``
    against the relevant Arr cache and, when that yields a non-``None``
    state, writes it onto ``item["download_state"]``.

    Self-healing of stale ``downloaded_at``: that column is stamped when the
    user adds a recommendation to Radarr/Sonarr and was never cleared when
    the media subsequently left Arr (deleted, abandoned). The card template
    and modal fall back to showing "Queued" off ``downloaded_at`` whenever
    no live ``download_state`` is present, so a long-gone item showed a
    phantom "Queued" badge forever. Here, when the relevant Arr client is
    configured *and* reports the item untracked, we clear the stale flag —
    both in memory (so this render is correct) and in the DB (*conn*, so it
    stays fixed). We only clear when the client is configured, so an
    Arr-down / unconfigured render never wipes a freshly-optimistic flag.

    The Radarr and Sonarr caches are built lazily — at most once each, and
    only if a movie / TV item is actually present — via *arr*. The two
    *empty* cache halves are built once up front rather than per iteration.

    Resilience: a recommendations page must not hard-depend on the arr stack
    being up. If building either cache raises an expected upstream failure —
    ``SafeHTTPError`` (transport: unreachable/timeout/5xx) or ``ArrError`` and
    its subclasses (domain) — that service degrades to an empty/unknown cache
    and the page still renders, just without that service's download badges.
    Each service fails independently (a down Sonarr does not hide Radarr state
    and vice versa). Programming errors propagate untouched.

    Returns a ``{item["id"]: item}`` map of every item seen, so the caller
    can serialise the full set without re-walking the batches.
    """
    empty_radarr = build_radarr_cache(None)
    empty_sonarr = build_sonarr_cache(None)

    radarr_cache: RadarrCaches | None = None
    sonarr_cache: SonarrCaches | None = None
    radarr_configured = False
    sonarr_configured = False

    stale_flag_ids: list[object] = []

    all_recs: dict[object, dict[str, object]] = {}
    for batch in batches:
        trending = batch["trending"]
        personal = batch["personal"]
        if not isinstance(trending, list) or not isinstance(personal, list):
            continue
        for item in trending + personal:
            tmdb_id = item.get("tmdb_id")
            media_type = item.get("media_type")
            # media_type comes straight off the recommendation dict and feeds
            # compute_download_state, where anything that isn't "movie" is
            # treated as a TV series. Validate it explicitly so a malformed or
            # unexpected value (e.g. "anime", "tv " with whitespace) is
            # skipped+logged rather than silently misclassified as Sonarr.
            if tmdb_id and isinstance(tmdb_id, int) and media_type in ("movie", "tv"):
                if media_type == "movie":
                    if radarr_cache is None:
                        # Build at most once; degrade to empty (no badges) if
                        # Radarr is unreachable so the page never 500s. See
                        # _build_radarr_cache_resilient.
                        radarr_cache, radarr_configured = _build_radarr_cache_resilient(
                            arr.radarr()
                        )
                    caches: ArrCaches = {**radarr_cache, **empty_sonarr}
                    configured = radarr_configured
                else:
                    if sonarr_cache is None:
                        # Build at most once; Sonarr fails independently of
                        # Radarr. See _build_sonarr_cache_resilient.
                        sonarr_cache, sonarr_configured = _build_sonarr_cache_resilient(
                            arr.sonarr()
                        )
                    caches = {**empty_radarr, **sonarr_cache}
                    configured = sonarr_configured
                state = compute_download_state(media_type, tmdb_id, caches)
                if state is not None:
                    item["download_state"] = state
                elif configured and item.get("downloaded_at"):
                    # Arr is reachable and no longer tracks this item — the
                    # download_at flag is stale. Clear it so the badge heals.
                    item["downloaded_at"] = None
                    stale_flag_ids.append(item["id"])
            elif tmdb_id and isinstance(tmdb_id, int) and media_type is not None:
                # Has a usable tmdb_id but a media_type we can't classify —
                # don't silently treat it as a series. Skip and log so the bad
                # value surfaces rather than producing a wrong/None state.
                logger.warning(
                    "attach_download_states: skipping item id=%r tmdb_id=%s — "
                    "unrecognised media_type %r (expected 'movie' or 'tv')",
                    item.get("id"),
                    tmdb_id,
                    media_type,
                )

            all_recs[item["id"]] = item

    if conn is not None:
        clear_stale_downloaded_at(conn, stale_flag_ids)

    return all_recs
