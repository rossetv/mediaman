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
from typing import TYPE_CHECKING

import requests

from mediaman.services.arr import ArrError
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
from mediaman.services.infra import ConfigDecryptError, SafeHTTPError

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


# A service's last full answer, which stands in for a single failed fetch after it: one missed
# answer is no outage, and the Downloads page polls every two seconds. An answer older than this
# stands in for nothing. Guarded by _answers_lock.
_STAND_IN_SECONDS = 60.0
_last_answers: dict[str, tuple[float, list[ArrCard]]] = {}
_stood_in: set[str] = set()
_answers_lock = threading.Lock()


def _forget_answers() -> None:
    with _answers_lock:
        _last_answers.clear()
        _stood_in.clear()


def fetch_arr_queue_result(
    conn: sqlite3.Connection, secret_key: str, *, steady: bool = True
) -> FetchResult:
    """Fetch Radarr/Sonarr queues and return a ``FetchResult``.

    ``secret_key`` is required to decrypt the Radarr/Sonarr API keys stored
    in DB settings.

    Unlike ``fetch_arr_queue``, this surfaces errors alongside cards so
    callers can show when a service is unreachable. While *steady*, a service
    that fails once, right after an answer less than a minute old, is given
    that answer's cards and not reported; the failure after that is. A refused
    key is reported at once.
    """
    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db

    result = FetchResult()
    for name, build, fetch in (
        ("Radarr", build_radarr_from_db, fetch_radarr_queue),
        ("Sonarr", build_sonarr_from_db, fetch_sonarr_queue),
    ):
        _fetch_service(conn, secret_key, name, build, fetch, result, steady=steady)
    return result


def _stand_in(name: str, result: FetchResult) -> bool:
    """Add *name*'s last answer to *result* for a fetch that failed, if it may stand in."""
    with _answers_lock:
        kept = _last_answers.get(name)
        if name in _stood_in or kept is None or time.monotonic() - kept[0] > _STAND_IN_SECONDS:
            return False
        _stood_in.add(name)
    result.cards.extend(kept[1])
    return True


def _keep_answer(name: str, cards: list[ArrCard]) -> None:
    with _answers_lock:
        _last_answers[name] = (time.monotonic(), list(cards))
        _stood_in.discard(name)


def _fetch_service(
    conn: sqlite3.Connection,
    secret_key: str,
    name: str,
    build: Callable[[sqlite3.Connection, str], ArrClient | None],
    fetch: Callable[..., list[ArrCard]],
    result: FetchResult,
    *,
    steady: bool,
) -> None:
    """Add one service's cards to *result*, or the error that kept them out; while *steady*,
    a single failure is answered by the service's last answer."""
    failures: list[str] = []
    try:
        client = build(conn, secret_key)
        if client is None:
            return
        cards = fetch(client, on_failure=lambda: failures.append(name))
    except (
        SafeHTTPError,
        requests.RequestException,
        ArrError,
        sqlite3.Error,
        ConfigDecryptError,
    ) as exc:
        logger.warning("Failed to fetch %s queue: %s", name, exc, exc_info=True)
        refused = isinstance(exc, SafeHTTPError) and exc.status_code in (401, 403)
        if steady and not refused and _stand_in(name, result):
            return
        result.errors.append(f"{name} fetch failed: {exc}")
        result.unreachable.append(name)
        if refused:
            result.refused.append(name)
        return
    if steady and failures and _stand_in(name, result):
        return
    result.unreachable.extend(failures)
    stamp_searching_since(conn, name.lower(), cards, complete=not failures)
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
