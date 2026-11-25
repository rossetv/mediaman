"""The library index Watch matches TMDB titles against, and the TMDB ids of library
shows known only by their TVDB id, both held in memory.

Building the index reads every library title, too slow to repeat on each discover row,
search, title open and recommended row, so it is held for ``INDEX_TTL_S`` seconds: a
show scanned since then shows within that time unless a scan ending or a title being
deleted calls ``clear_library_index()``, which drops it at once. A show without a TMDB id of its own
matches by the one TMDB gives its TVDB id. TMDB's answer is held, a match for
``MATCH_TTL_S`` and no match for ``NO_MATCH_TTL_S``; a failed ask is held as unknown for
``RETRY_S``. Building never waits on TMDB: an id not known yet, or held past its time, is
asked in the background, one at a time. A match held past its time keeps being used
until the new answer replaces it. The next caller builds afresh once a batch of asks
has learnt something, not after each answer.
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
from ._discover import _failure
from ._gate import TmdbUnavailable, gate
from ._title_meta import clear_held

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
# Bumped when the index is cleared or a batch of asks changed a match, so an index built
# before it is built again.
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


def clear_library_index() -> None:
    """Drop the held index, and the titles and people read of the library, so the next
    caller builds them afresh. For a library scan that
    has finished and a library title that was deleted."""
    with _lock:
        _held.clear()
        _generation[0] += 1
    clear_held()


def tmdb_id_for_tvdb(client: TmdbClient, tvdb_id: int) -> int | None:
    """The TMDB id of the show with this TVDB id: the one held, else TMDB's answer now
    (the match held past its time when TMDB fails); None when TMDB has none or failed."""
    with _lock:
        held = _tvdb.get(tvdb_id)
    if held is not None and held[0] > _clock():
        return held[1]
    try:
        with gate.asking(client.token):
            found, changed = _ask(client, tvdb_id)
    except TmdbUnavailable:
        return held[1] if held is not None else None
    if changed:
        with _lock:
            _generation[0] += 1
    return found


def held_tmdb_id_for_tvdb(tvdb_id: int) -> int | None:
    """The TMDB id held of the show with this TVDB id. TMDB is not asked."""
    return _known([tvdb_id])[0].get(tvdb_id)


def _current(key: tuple[str, tuple[str, ...]]) -> LibraryIndex | None:
    with _lock:
        held = _held.get(key)
        if held is None or held[0] <= _clock() or held[1] != _generation[0]:
            return None
        return held[2]


def _known(tvdb_ids: Iterable[int]) -> tuple[dict[int, int], set[int]]:
    """(TMDB id by TVDB id held, TVDB ids to ask: none held, or held past its time).
    A match held past its time is in both."""
    now = _clock()
    known: dict[int, int] = {}
    unknown: set[int] = set()
    with _lock:
        for tvdb_id in tvdb_ids:
            held = _tvdb.get(tvdb_id)
            if held is None or held[0] <= now:
                unknown.add(tvdb_id)
            if held is not None and held[1] is not None:
                known[tvdb_id] = held[1]
    return known, unknown


def _ask(client: TmdbClient, tvdb_id: int) -> tuple[int | None, bool]:
    """Ask TMDB for the TMDB id of *tvdb_id* and hold the answer: (the TMDB id held now,
    whether it is not the one held before). A failed ask keeps the match held before."""
    try:
        answer = client.find_by_tvdb(tvdb_id)
        failed = False
        gate.note(client.token, None)
    except (SafeHTTPError, requests.RequestException, ValueError) as exc:
        answer, failed = None, True
        gate.note(client.token, _failure(exc, "find"))
    with _lock:
        before = _tvdb.get(tvdb_id, (0.0, None))[1]
        if failed:
            found, ttl = before, RETRY_S
        else:
            found = answer
            ttl = MATCH_TTL_S if found is not None else NO_MATCH_TTL_S
        _tvdb[tvdb_id] = (_clock() + ttl, found)
    return found, found != before


def _ask_later(client: TmdbClient, tvdb_ids: set[int]) -> None:
    with _lock:
        todo = sorted(tvdb_ids - _asking)
        _asking.update(todo)
    if not todo:
        return

    def run() -> None:
        changed = False
        try:
            for tvdb_id in todo:
                changed |= _ask(client, tvdb_id)[1]
        except Exception:
            logger.warning("watch: asking TMDB for TVDB ids failed", exc_info=True)
        finally:
            with _lock:
                _asking.difference_update(todo)
                if changed:
                    _generation[0] += 1

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
