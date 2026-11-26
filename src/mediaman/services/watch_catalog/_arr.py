"""Radarr and Sonarr state for Watch, held for 30 seconds.

Both libraries are pulled whole on each build, which is too slow to repeat on every
discover row, search and title open. A download started from Watch calls
``invalidate()`` so the next answer shows it queued rather than the held state.
A service that cannot be reached counts as empty: its titles carry no download state.
A Sonarr that gave no answer is asked again sooner, alone. While a service gives none, the view is
built behind the pages, which are given what is held.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from typing import cast

from mediaman.services.arr import build as arr_build
from mediaman.services.arr._types import ArrQueueItem, SonarrSeries
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.seasons import (
    SeasonQueue,
    ShowSeasons,
    queued_seasons,
    readable_queue,
    readable_seasons,
    show_seasons,
)
from mediaman.services.arr.state import (
    ACTION_DOWNLOADING,
    ACTION_QUEUED,
    ArrCaches,
    RadarrCaches,
    SonarrCaches,
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
)

logger = logging.getLogger(__name__)

TTL_S = 30.0
#: How long a Sonarr that gave no answer is left alone, and how long a page waits for a service
#: that gave none.
RETRY_S = 5.0
RETRY_WAIT_S = 1.0
#: How long past its time a view still comes back for that, and how long from when what takes the
#: place of an older one began pages wait for it.
OLD_S = 300.0
OLD_WAIT_S = 10.0

_lock = threading.Lock()
_memo: dict[str, _Held] = {}
# Bumped by invalidate(), so a build that started before it is not kept.
_generation = [0]
# The build in progress for each generation, and when it began; a caller of the same generation
# waits for it rather than pulling both libraries again.
_building: dict[int, tuple[Future[ArrView], float]] = {}
# The last build begun that no caller waits out.
_background: list[Future[ArrView]] = []


@dataclass(frozen=True, slots=True)
class ArrView:
    caches: ArrCaches
    #: The services ("radarr", "sonarr") set up and answering, so a title missing from
    #: one is truly not tracked there.
    answering: frozenset[str] = frozenset()
    #: The services not set up, unlike one that is and gave no answer.
    not_set_up: frozenset[str] = frozenset()
    #: The generation of the build that made this view; None for one not built here.
    generation: int | None = None
    season_queue: SeasonQueue = field(default_factory=dict)
    #: The TMDB ids of shows Sonarr gave a season of that cannot be read.
    odd_shows: frozenset[int] = frozenset()

    def is_current(self) -> bool:
        """Whether no invalidate has happened since this view was built."""
        return self.generation is not None and self.generation == _generation[0]

    def answers_for(self, kind: str) -> bool:
        """Whether the service that would fetch a title of *kind* answered."""
        return ("radarr" if kind == "movie" else "sonarr") in self.answering

    def state(self, kind: str, tmdb_id: int) -> tuple[str | None, int | None]:
        """(download state, percent done) of a TMDB title; the percent only while queued or
        downloading."""
        media_type = "movie" if kind == "movie" else "tv"
        state = compute_download_state(media_type, tmdb_id, self.caches)
        if state not in (ACTION_QUEUED, ACTION_DOWNLOADING):
            return state, None
        if media_type == "movie":
            progress = self.caches.get("radarr_queue_progress", {})
        else:
            progress = self.caches.get("sonarr_queue_progress", {})
        return state, progress.get(tmdb_id, 0)

    def sonarr_tracked(self, tmdb_id: int) -> bool:
        return tmdb_id in self.caches["sonarr_series"]

    def tvdb_id(self, tmdb_id: int) -> int | None:
        tvdb = self.caches["sonarr_series"].get(tmdb_id, {}).get("tvdbId")
        return tvdb if isinstance(tvdb, int) else None

    def tmdb_id_for_tvdb(self, tvdb_id: int) -> int | None:
        return next(
            (tid for tid, s in self.caches["sonarr_series"].items() if s.get("tvdbId") == tvdb_id),
            None,
        )

    def show_seasons(
        self, tmdb_id: int | None, tvdb_id: int | None, air_dates: Mapping[int, str | None]
    ) -> ShowSeasons | None:
        """What Sonarr has of a show's seasons, as ``seasons.show_seasons``; None for a show it
        does not track."""
        held = self.caches["sonarr_series"]
        key = tmdb_id if tmdb_id in held else None
        if key is None and tvdb_id:
            key = self.tmdb_id_for_tvdb(tvdb_id)
        if key is None:
            return None
        found = show_seasons(held[key], self.season_queue, air_dates)
        return replace(found, whole=found.whole and key not in self.odd_shows)

    def seasons_with_files(self, tmdb_id: int) -> set[int]:
        series = self.caches["sonarr_series"].get(tmdb_id, {})
        return {
            number
            for season in series.get("seasons", [])
            if (season.get("statistics") or {}).get("episodeFileCount", 0) > 0
            and (number := season.get("seasonNumber")) is not None
        }


@dataclass(frozen=True, slots=True)
class _Held:
    view: ArrView
    #: When both are asked again, and when Sonarr alone is if that is sooner.
    until: float
    again: float

    def lacks_an_answer(self) -> bool:
        """Whether a service that is set up gave none."""
        return not {"radarr", "sonarr"} <= self.view.answering | self.view.not_set_up


def _client(
    build: Callable[[sqlite3.Connection, str], ArrClient | None],
    conn: sqlite3.Connection,
    secret_key: str,
) -> ArrClient | None:
    try:
        return build(conn, secret_key)
    except Exception:
        logger.warning("watch: an arr client cannot be set up", exc_info=True)
        return None


def _radarr(client: ArrClient | None) -> tuple[RadarrCaches, bool]:
    """Radarr's caches, and whether it is set up and answered."""
    try:
        return build_radarr_cache(client), client is not None
    except Exception:
        logger.warning("watch: Radarr unavailable; titles show no Radarr state", exc_info=True)
        return build_radarr_cache(None), False


class _SonarrRead:
    """A Sonarr client for the cache builder: gives it what has the shape it reads, and keeps
    what the builder has no place for."""

    def __init__(self, client: ArrClient) -> None:
        self._client = client
        self.queue: list[ArrQueueItem] = []
        self.odd_shows: set[int] = set()

    def get_series(self) -> list[SonarrSeries]:
        found = []
        for series in self._client.get_series():
            tmdb_id = series.get("tmdbId")
            seasons, whole = readable_seasons(series)
            if not whole and isinstance(tmdb_id, int):
                self.odd_shows.add(tmdb_id)
            if "seasons" in series and not whole:
                series = {**series, "seasons": seasons}
            if tmdb_id is None or isinstance(tmdb_id, int):
                found.append(series)
        return found

    def get_queue(self) -> list[ArrQueueItem]:
        self.queue = readable_queue(self._client.get_queue())
        return self.queue


@dataclass(frozen=True, slots=True)
class _Sonarr:
    caches: SonarrCaches
    queue: SeasonQueue
    odd_shows: frozenset[int]
    answered: bool


def _sonarr(client: ArrClient | None) -> _Sonarr:
    """What Sonarr holds, and whether it is set up and answered."""
    none = _Sonarr(build_sonarr_cache(None), {}, frozenset(), False)
    if client is None:
        return none
    try:
        read = _SonarrRead(client)
        caches = build_sonarr_cache(cast(ArrClient, read))
        return _Sonarr(caches, queued_seasons(read.queue), frozenset(read.odd_shows), True)
    except Exception:
        logger.warning("watch: Sonarr unavailable; titles show no Sonarr state", exc_info=True)
        return none


def arr_view(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    wait_s: float | None = None,
) -> ArrView:
    """The held view, or one built now. With *wait_s* a view not there by then goes on being
    built in the background, and an empty one that answers for neither service comes back; one
    is built there at a time, however often the view is dropped meanwhile. While a service gives
    no answer it is asked there too, and what is held comes back without *wait_s* when it is slow."""
    late = ArrView({**build_radarr_cache(None), **build_sonarr_cache(None)})
    with _lock:
        held, now = _memo.get("arr"), clock()
        if held is not None and now < held.again:
            return held.view
        if held is not None and not held.lacks_an_answer():
            held = None
        old = held is not None and wait_s is None and now >= held.until + OLD_S
        if held is not None and wait_s is None:
            late, wait_s = held.view, RETRY_WAIT_S
        generation = _generation[0]
        future, began = _building.get(generation, (None, now))
        owner = future is None
        if future is None:
            if wait_s is not None and any(not behind.done() for behind in _background):
                return late
            future = Future()
            _building[generation] = (future, now)
            if wait_s is not None:
                _background[:] = [future]
        if old:
            wait_s = max(RETRY_WAIT_S, began + OLD_WAIT_S - now)
    if owner:
        try:
            # Built here: the connection is this thread's own.
            if held is not None and held.until <= now:
                held = None
            radarr = None if held else _client(arr_build.build_radarr_from_db, conn, secret_key)
            sonarr = _client(arr_build.build_sonarr_from_db, conn, secret_key)
            args = (future, generation, radarr, sonarr, clock, held)
            if wait_s is None:
                _build(*args)
            else:
                threading.Thread(target=_build, args=args, name="watch-arr", daemon=True).start()
        except BaseException as exc:
            _failed(future, generation, exc)
    try:
        return future.result(wait_s)
    except TimeoutError:
        return late


def _build(
    future: Future[ArrView],
    generation: int,
    radarr_client: ArrClient | None,
    sonarr_client: ArrClient | None,
    clock: Callable[[], float],
    held: _Held | None,
) -> None:
    """Build the view; with *held* from what that has of Radarr."""
    try:
        radarr, radarr_answered = _radarr(radarr_client)
        sonarr = _sonarr(sonarr_client)
    except BaseException as exc:
        _failed(future, generation, exc)
        return
    caches: ArrCaches = {**radarr, **sonarr.caches}
    radarr_set_up = radarr_client is not None
    if held is not None:
        caches = {**held.view.caches, **sonarr.caches}
        radarr_answered = "radarr" in held.view.answering
        radarr_set_up = "radarr" not in held.view.not_set_up
    answering = frozenset(
        name for name, ok in (("radarr", radarr_answered), ("sonarr", sonarr.answered)) if ok
    )
    set_up = (("radarr", radarr_set_up), ("sonarr", sonarr_client is not None))
    not_set_up = frozenset(name for name, ok in set_up if not ok)
    view = ArrView(caches, answering, not_set_up, generation, sonarr.queue, sonarr.odd_shows)
    known = "sonarr" in answering | not_set_up
    with _lock:
        if _building.get(generation, (None,))[0] is future:
            del _building[generation]
            if _generation[0] == generation:
                now = clock()
                until = max(held.until, now + RETRY_S) if held else now + TTL_S
                _memo["arr"] = _Held(view, until, until if known else now + RETRY_S)
    future.set_result(view)


def _failed(future: Future[ArrView], generation: int, exc: BaseException) -> None:
    with _lock:
        if _building.get(generation, (None,))[0] is future:
            del _building[generation]
    future.set_exception(exc)


def invalidate() -> None:
    """Forget the held state, so the next answer asks Radarr and Sonarr again."""
    with _lock:
        _memo.clear()
        _generation[0] += 1
