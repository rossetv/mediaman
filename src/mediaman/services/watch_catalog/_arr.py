"""Radarr and Sonarr state for Watch, held for 30 seconds.

Both libraries are pulled whole on each build, which is too slow to repeat on every
discover row, search and title open. A download started from Watch calls
``invalidate()`` so the next answer shows it queued rather than the held state.
A service that cannot be reached counts as empty: its titles carry no download state.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import cast

import requests

from mediaman.services.arr import ArrError
from mediaman.services.arr import build as arr_build
from mediaman.services.arr._types import ArrQueueItem, SonarrSeries
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.seasons import SeasonQueue, SeasonState, queued_seasons, season_states
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
from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)

TTL_S = 30.0
_ARR_ERRORS = (SafeHTTPError, requests.RequestException, ArrError, sqlite3.Error, ValueError)

_lock = threading.Lock()
_memo: dict[str, tuple[float, ArrView]] = {}
# Bumped by invalidate(), so a build that started before it is not kept.
_generation = [0]
# The build in progress for each generation; a caller of the same generation waits for
# it rather than pulling both libraries again.
_building: dict[int, Future[ArrView]] = {}


@dataclass(frozen=True, slots=True)
class ArrView:
    caches: ArrCaches
    #: The services ("radarr", "sonarr") set up and answering, so a title missing from
    #: one is truly not tracked there.
    answering: frozenset[str] = frozenset()
    #: The generation of the build that made this view; None for one not built here.
    generation: int | None = None
    season_queue: SeasonQueue = field(default_factory=dict)

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

    def season_states(
        self, tmdb_id: int | None, tvdb_id: int | None
    ) -> dict[int, SeasonState] | None:
        """The state of each season Sonarr lists of a show; None for one it does not track."""
        held = self.caches["sonarr_series"]
        series = held.get(tmdb_id) if tmdb_id else None
        if series is None and tvdb_id:
            series = next((s for s in held.values() if s.get("tvdbId") == tvdb_id), None)
        return season_states(series, self.season_queue) if series is not None else None

    def seasons_with_files(self, tmdb_id: int) -> set[int]:
        series = self.caches["sonarr_series"].get(tmdb_id, {})
        return {
            number
            for season in series.get("seasons", [])
            if (season.get("statistics") or {}).get("episodeFileCount", 0) > 0
            and (number := season.get("seasonNumber")) is not None
        }


def _client(
    build: Callable[[sqlite3.Connection, str], ArrClient | None],
    conn: sqlite3.Connection,
    secret_key: str,
) -> ArrClient | None:
    try:
        return build(conn, secret_key)
    except _ARR_ERRORS:
        logger.warning("watch: an arr client cannot be set up", exc_info=True)
        return None


def _radarr(client: ArrClient | None) -> tuple[RadarrCaches, bool]:
    """Radarr's caches, and whether it is set up and answered."""
    try:
        return build_radarr_cache(client), client is not None
    except _ARR_ERRORS:
        logger.warning("watch: Radarr unavailable; titles show no Radarr state", exc_info=True)
        return build_radarr_cache(None), False


class _QueueKept:
    """A Sonarr client for the cache builder, which keeps no queue by season: keeps the one read."""

    def __init__(self, client: ArrClient) -> None:
        self._client = client
        self.queue: list[ArrQueueItem] = []

    def get_series(self) -> list[SonarrSeries]:
        return self._client.get_series()

    def get_queue(self) -> list[ArrQueueItem]:
        self.queue = self._client.get_queue()
        return self.queue


def _sonarr(client: ArrClient | None) -> tuple[SonarrCaches, SeasonQueue, bool]:
    """Sonarr's caches, its queue by season, and whether it is set up and answered."""
    if client is None:
        return build_sonarr_cache(None), {}, False
    try:
        kept = _QueueKept(client)
        caches = build_sonarr_cache(cast(ArrClient, kept))
        return caches, queued_seasons(kept.queue), True
    except _ARR_ERRORS:
        logger.warning("watch: Sonarr unavailable; titles show no Sonarr state", exc_info=True)
        return build_sonarr_cache(None), {}, False


def arr_view(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    wait_s: float | None = None,
) -> ArrView:
    """The held view, or one built now. With *wait_s* a view not there by then is built on in
    the background, and an empty one that answers for neither service comes back."""
    with _lock:
        held = _memo.get("arr")
        if held is not None and held[0] > clock():
            return held[1]
        generation = _generation[0]
        future = _building.get(generation)
        owner = future is None
        if future is None:
            future = _building[generation] = Future()
    if owner:
        try:
            # Built here: the connection is this thread's own.
            radarr = _client(arr_build.build_radarr_from_db, conn, secret_key)
            sonarr = _client(arr_build.build_sonarr_from_db, conn, secret_key)
            args = (future, generation, radarr, sonarr, clock)
            if wait_s is None:
                _build(*args)
            else:
                threading.Thread(target=_build, args=args, daemon=True).start()
        except BaseException as exc:
            _failed(future, generation, exc)
    try:
        return future.result(wait_s)
    except TimeoutError:
        return ArrView({**build_radarr_cache(None), **build_sonarr_cache(None)})


def _build(
    future: Future[ArrView],
    generation: int,
    radarr_client: ArrClient | None,
    sonarr_client: ArrClient | None,
    clock: Callable[[], float],
) -> None:
    try:
        radarr, radarr_answered = _radarr(radarr_client)
        sonarr, season_queue, sonarr_answered = _sonarr(sonarr_client)
    except BaseException as exc:
        _failed(future, generation, exc)
        return
    answering = frozenset(
        name for name, ok in (("radarr", radarr_answered), ("sonarr", sonarr_answered)) if ok
    )
    view = ArrView({**radarr, **sonarr}, answering, generation, season_queue)
    with _lock:
        if _generation[0] == generation:
            _memo["arr"] = (clock() + TTL_S, view)
        _building.pop(generation, None)
    future.set_result(view)


def _failed(future: Future[ArrView], generation: int, exc: BaseException) -> None:
    with _lock:
        _building.pop(generation, None)
    future.set_exception(exc)


def invalidate() -> None:
    """Forget the held state, so the next answer asks Radarr and Sonarr again."""
    with _lock:
        _memo.clear()
        _generation[0] += 1
