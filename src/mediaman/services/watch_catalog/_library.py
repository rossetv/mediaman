"""The library index Watch matches TMDB titles against, and the TMDB ids of library
shows known only by their TVDB id, both held in memory.

Building the index reads every library title, too slow to repeat on each discover row,
search, title open and recommended row, so it is held for ``INDEX_TTL_S`` seconds: a
show scanned since then shows within that time. A show without a TMDB id of its own
matches by the one TMDB gives its TVDB id. TMDB's answer is held, a match for
``MATCH_TTL_S`` and no match for ``NO_MATCH_TTL_S``; a failed ask is held as unknown for
``RETRY_S``. Building never waits on TMDB: an id not known yet is asked in the
background, one at a time, and a match learnt makes the next caller build afresh.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.tmdb import TmdbClient

from ._dedupe import LibraryIndex, library_ids

logger = logging.getLogger(__name__)

_DAY = 24 * 60 * 60.0
INDEX_TTL_S = 60.0
MATCH_TTL_S = 30 * _DAY
NO_MATCH_TTL_S = _DAY
RETRY_S = 60.0

#: Run the background asks inline (tests).
sync = False

_clock: Callable[[], float] = time.monotonic

_lock = threading.Lock()
# Held while building, so callers that find the index out of date build it once.
_build_lock = threading.Lock()
# TVDB id -> (held until, TMDB id); None is no match, or unknown after a failed ask.
_tvdb: dict[int, tuple[float, int | None]] = {}
# TVDB ids waiting for, or in, a background ask.
_asking: set[int] = set()
# Bumped when a match is learnt, so an index built before it is built again.
_generation = [0]
# (database file, libraries) -> (held until, generation, index); one entry at most.
_held: dict[tuple[str, tuple[str, ...]], tuple[float, int, LibraryIndex]] = {}
_pool: ThreadPoolExecutor | None = None


class _Movie(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def tmdb_id(self) -> int | None: ...


class _Show(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def tvdb_id(self) -> int | None: ...


def library_index(
    conn: sqlite3.Connection,
    client: TmdbClient | None,
    libraries: Sequence[str],
    movies: Callable[[sqlite3.Connection, list[str]], Iterable[_Movie]],
    shows: Callable[[sqlite3.Connection, list[str]], Iterable[_Show]],
) -> LibraryIndex:
    """The index of the titles *movies* and *shows* list in *libraries*, built afresh
    only once the one held is out of date. Without a TMDB *client* the TVDB ids not
    known yet are left unasked."""
    key = (str(conn.execute("PRAGMA database_list").fetchone()[2]), tuple(libraries))
    index = _current(key)
    if index is not None:
        return index
    with _build_lock:
        index = _current(key)
        if index is not None:
            return index
        with _lock:
            generation = _generation[0]
        listed = list(shows(conn, list(libraries)))
        ids = library_ids(conn)
        wanted = {
            s.tvdb_id for s in listed if s.tvdb_id and not ids.get(f"show-{s.key}", (None,))[0]
        }
        known, unknown = _known(wanted)
        index = LibraryIndex.build(
            ((m.key, m.title, m.tmdb_id) for m in movies(conn, list(libraries))),
            ((s.key, s.title, s.tvdb_id) for s in listed),
            ids=ids,
            tmdb_for_tvdb=known,
        )
        with _lock:
            _held.clear()
            _held[key] = (_clock() + INDEX_TTL_S, generation, index)
    if client is not None and unknown:
        _ask_later(client, unknown)
    return index


def tmdb_id_for_tvdb(client: TmdbClient, tvdb_id: int) -> int | None:
    """The TMDB id of the show with this TVDB id: the one held, else TMDB's answer now;
    None when TMDB has none or failed."""
    with _lock:
        held = _tvdb.get(tvdb_id)
    if held is not None and held[0] > _clock():
        return held[1]
    return _ask(client, tvdb_id)


def _current(key: tuple[str, tuple[str, ...]]) -> LibraryIndex | None:
    with _lock:
        held = _held.get(key)
        if held is None or held[0] <= _clock() or held[1] != _generation[0]:
            return None
        return held[2]


def _known(tvdb_ids: Iterable[int]) -> tuple[dict[int, int], set[int]]:
    """(TMDB id by TVDB id held, TVDB ids with nothing held)."""
    now = _clock()
    known: dict[int, int] = {}
    unknown: set[int] = set()
    with _lock:
        for tvdb_id in tvdb_ids:
            held = _tvdb.get(tvdb_id)
            if held is None or held[0] <= now:
                unknown.add(tvdb_id)
            elif held[1] is not None:
                known[tvdb_id] = held[1]
    return known, unknown


def _ask(client: TmdbClient, tvdb_id: int) -> int | None:
    """Ask TMDB for the TMDB id of *tvdb_id* and hold the answer."""
    try:
        found = client.find_by_tvdb(tvdb_id)
    except (SafeHTTPError, requests.RequestException, ValueError):
        found, ttl = None, RETRY_S
    else:
        ttl = MATCH_TTL_S if found is not None else NO_MATCH_TTL_S
    with _lock:
        _tvdb[tvdb_id] = (_clock() + ttl, found)
        if found is not None:
            _generation[0] += 1
    return found


def _ask_later(client: TmdbClient, tvdb_ids: set[int]) -> None:
    with _lock:
        todo = sorted(tvdb_ids - _asking)
        _asking.update(todo)
    if not todo:
        return

    def run() -> None:
        try:
            for tvdb_id in todo:
                _ask(client, tvdb_id)
        except Exception:
            logger.warning("watch: asking TMDB for TVDB ids failed", exc_info=True)
        finally:
            with _lock:
                _asking.difference_update(todo)

    if sync:
        run()
        return
    try:
        _executor().submit(run)
    except RuntimeError:  # the pool is shut down (interpreter exit)
        with _lock:
            _asking.difference_update(todo)


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watch-tvdb")
        return _pool
