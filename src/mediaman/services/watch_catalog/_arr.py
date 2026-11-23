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
from dataclasses import dataclass

import requests

from mediaman.services.arr import ArrError
from mediaman.services.arr import build as arr_build
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
_memo: dict[str, tuple[float, ArrCaches]] = {}
# Bumped by invalidate(), so a build that started before it is not kept.
_generation = [0]
# The build in progress for each generation; a caller of the same generation waits for
# it rather than pulling both libraries again.
_building: dict[int, Future[ArrCaches]] = {}


@dataclass(frozen=True, slots=True)
class ArrView:
    caches: ArrCaches

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

    def seasons_with_files(self, tmdb_id: int) -> set[int]:
        series = self.caches["sonarr_series"].get(tmdb_id, {})
        return {
            number
            for season in series.get("seasons", [])
            if (season.get("statistics") or {}).get("episodeFileCount", 0) > 0
            and (number := season.get("seasonNumber")) is not None
        }


def _radarr(conn: sqlite3.Connection, secret_key: str) -> RadarrCaches:
    try:
        return build_radarr_cache(arr_build.build_radarr_from_db(conn, secret_key))
    except _ARR_ERRORS:
        logger.warning("watch: Radarr unavailable; titles show no Radarr state", exc_info=True)
        return build_radarr_cache(None)


def _sonarr(conn: sqlite3.Connection, secret_key: str) -> SonarrCaches:
    try:
        return build_sonarr_cache(arr_build.build_sonarr_from_db(conn, secret_key))
    except _ARR_ERRORS:
        logger.warning("watch: Sonarr unavailable; titles show no Sonarr state", exc_info=True)
        return build_sonarr_cache(None)


def arr_view(
    conn: sqlite3.Connection, secret_key: str, *, clock: Callable[[], float] = time.monotonic
) -> ArrView:
    now = clock()
    with _lock:
        held = _memo.get("arr")
        if held is not None and held[0] > now:
            return ArrView(held[1])
        generation = _generation[0]
        future = _building.get(generation)
        owner = future is None
        if future is None:
            future = _building[generation] = Future()
    if not owner:
        return ArrView(future.result())
    try:
        caches: ArrCaches = {**_radarr(conn, secret_key), **_sonarr(conn, secret_key)}
    except BaseException as exc:
        with _lock:
            _building.pop(generation, None)
        future.set_exception(exc)
        raise
    with _lock:
        if _generation[0] == generation:
            _memo["arr"] = (now + TTL_S, caches)
        _building.pop(generation, None)
    future.set_result(caches)
    return ArrView(caches)


def invalidate() -> None:
    """Forget the held state, so the next answer asks Radarr and Sonarr again."""
    with _lock:
        _memo.clear()
        _generation[0] += 1
