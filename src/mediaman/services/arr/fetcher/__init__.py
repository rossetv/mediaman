"""arr_fetcher package — Radarr/Sonarr queue fetch.

Public entry points:

- ``fetch_arr_queue`` -- returns a plain list of cards.
- ``fetch_arr_queue_result`` -- returns a ``FetchResult`` that also
  carries any fetch errors and the names of the services that failed.

All public symbols are re-exported here so imports such as
``from mediaman.services.arr.fetcher import FetchResult, fetch_arr_queue``
work.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from mediaman.services.arr import _library_hold
from mediaman.services.arr._throttle_persistence import adopt_throttle
from mediaman.services.arr.fetcher._base import (
    ArrCard,
    ArrEpisodeEntry,
    BaseArrCard,
    FetchResult,
)
from mediaman.services.arr.fetcher._radarr import fetch_radarr_queue
from mediaman.services.arr.fetcher._searching_since import stamp_searching_since
from mediaman.services.arr.fetcher._sonarr import fetch_sonarr_queue
from mediaman.services.infra import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)

__all__ = [
    "ArrCard",
    "ArrEpisodeEntry",
    "BaseArrCard",
    "FetchResult",
    "fetch_arr_queue",
    "fetch_arr_queue_result",
]


# A service's last full answer, which stands in for the fetches that fail in the first few
# seconds after one fails: a blip is no outage, and every open Downloads page and header strip
# polls on its own. An answer older than a minute stands in for nothing. Guarded by _answers_lock.
_STAND_IN_SECONDS = 60.0
_BLIP_SECONDS = 5.0
_last_answers: dict[str, tuple[float, list[ArrCard]]] = {}
_failing_since: dict[str, float] = {}
_answers_lock = threading.Lock()


def _forget_answers() -> None:
    with _answers_lock:
        _last_answers.clear()
        _failing_since.clear()
    _library_hold.forget()


class _HeldLibrary:
    """*client* for a poll: its queue read live, its library as _library_hold holds it."""

    def __init__(self, client: ArrClient) -> None:
        self._client = client
        self._queue: frozenset[object] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def get_queue(self) -> list[Any]:
        entries = self._client.get_queue(quick=True)
        self._queue = _library_hold.queue_of(entries)
        return entries

    def get_movies(self) -> list[Any]:
        return self._held("movies", self._client.get_movies)

    def get_series(self) -> list[Any]:
        return self._held("series", self._client.get_series)

    def get_episodes(self, series_id: int) -> list[Any]:
        return self._held(f"episodes:{series_id}", lambda: self._client.get_episodes(series_id))

    def _held(self, what: str, read: Callable[[], list[Any]]) -> list[Any]:
        return _library_hold.held_read(self._client._url, what, self._queue, read)


def fetch_arr_queue_result(
    conn: sqlite3.Connection, secret_key: str, *, steady: bool = True
) -> FetchResult:
    """Fetch Radarr/Sonarr queues and return a ``FetchResult``.

    ``secret_key`` is required to decrypt the Radarr/Sonarr API keys stored
    in DB settings.

    Unlike ``fetch_arr_queue``, this surfaces errors alongside cards so
    callers can show when a service is unreachable. The services are read at
    the same time, and a poll, while *steady*, gives each queue page one short
    try, so a service that hangs holds back no other. One that fails for any
    reason is reported alone, the other's cards kept. While *steady*, a service
    that fails within five seconds of its first failure in a row is given its
    last answer's cards, if that answer is less than a minute old, and not
    reported; a failure after that is. A refused key is reported at once.
    """
    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db

    result = FetchResult()
    reads: list[tuple[str, Future[list[ArrCard]], list[str]]] = []
    # The database is read and written here alone: the connection is this thread's.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="arr-fetch") as pool:
        for name, build, fetch in (
            ("Radarr", build_radarr_from_db, fetch_radarr_queue),
            ("Sonarr", build_sonarr_from_db, fetch_sonarr_queue),
        ):
            try:
                client = build(conn, secret_key)
            except Exception as exc:  # rationale: one service's failure must not hide the other's
                _failed(name, exc, result, steady=steady)
                continue
            if client is None:
                continue
            # A poll reads the library as held; a caller that acts on what it reads reads it live.
            polled = cast("ArrClient", _HeldLibrary(client)) if steady else client
            failures: list[str] = []
            reads.append(
                (
                    name,
                    pool.submit(fetch, polled, on_failure=partial(failures.append, name)),
                    failures,
                )
            )
    for name, read, failures in reads:
        try:
            cards = read.result()
        except Exception as exc:  # rationale: one service's failure must not hide the other's
            _failed(name, exc, result, steady=steady)
            continue
        _add_cards(conn, name, cards, failures, result, steady=steady)
    return result


def _stand_in(name: str, result: FetchResult) -> bool:
    """Add *name*'s last answer to *result* for a fetch that failed, if it may stand in."""
    now = time.monotonic()
    with _answers_lock:
        kept = _last_answers.get(name)
        blip = now - _failing_since.setdefault(name, now)
        if kept is None or now - kept[0] > _STAND_IN_SECONDS or blip > _BLIP_SECONDS:
            return False
    result.cards.extend(kept[1])
    result.stood_in.append(name)
    return True


def _keep_answer(name: str, cards: list[ArrCard]) -> None:
    with _answers_lock:
        _last_answers[name] = (time.monotonic(), list(cards))
        _failing_since.pop(name, None)


def _failed(name: str, exc: Exception, result: FetchResult, *, steady: bool) -> None:
    """Report that *name* gave no cards, for *exc*; while *steady*, a failure in a blip is
    answered by the service's last answer."""
    logger.warning("Failed to fetch %s queue: %s", name, exc, exc_info=True)
    refused = isinstance(exc, SafeHTTPError) and exc.status_code in (401, 403)
    if steady and not refused and _stand_in(name, result):
        return
    result.errors.append(f"{name} fetch failed: {exc}")
    result.unreachable.append(name)
    if refused:
        result.refused.append(name)


def _add_cards(
    conn: sqlite3.Connection,
    name: str,
    cards: list[ArrCard],
    failures: list[str],
    result: FetchResult,
    *,
    steady: bool,
) -> None:
    """Add one service's *cards* to *result*, those of a read with *failures* incomplete."""
    # Here, not at the top: import_retry imports this package's _base.
    from mediaman.services.arr.import_retry import note_stuck_imports

    if steady and failures and _stand_in(name, result):
        return
    result.unreachable.extend(failures)
    stamp_searching_since(conn, name.lower(), cards, complete=not failures)
    note_stuck_imports(conn, name.lower(), cards, complete=not failures)
    adopt_throttle(conn, cards)
    if steady and not failures:
        _keep_answer(name, cards)
    result.cards.extend(cards)


def fetch_arr_queue(conn: sqlite3.Connection, secret_key: str) -> list[ArrCard]:
    """Fetch Radarr/Sonarr queues, grouping Sonarr episodes by series.

    Returns a list of download cards.  Movies are one card each.
    TV series are grouped into a single card with an ``episodes`` list.

    This is the wrapper around ``fetch_arr_queue_result`` that drops its error report.
    Callers that need to surface fetch errors to the UI should use that function
    instead.
    """
    return fetch_arr_queue_result(conn, secret_key, steady=False).cards
