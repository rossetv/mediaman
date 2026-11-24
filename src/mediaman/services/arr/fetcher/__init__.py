"""arr_fetcher package — Radarr/Sonarr queue fetch.

Public entry points:

- ``fetch_arr_queue`` -- returns a plain list of cards.
- ``fetch_arr_queue_result`` -- returns a ``FetchResult`` that also
  carries any fetch errors so the UI can display a banner.

All public symbols are re-exported here so imports such as
``from mediaman.services.arr.fetcher import FetchResult, fetch_arr_queue``
work.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

import requests

from mediaman.services.arr import ArrError
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


def fetch_arr_queue_result(conn: sqlite3.Connection, secret_key: str) -> FetchResult:
    """Fetch Radarr/Sonarr queues and return a ``FetchResult``.

    ``secret_key`` is required to decrypt the Radarr/Sonarr API keys stored
    in DB settings.

    Unlike ``fetch_arr_queue``, this surfaces errors alongside cards so
    callers can show a UI banner when a service is unreachable.
    """
    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db

    result = FetchResult()
    _fetch_service(conn, secret_key, "Radarr", build_radarr_from_db, fetch_radarr_queue, result)
    _fetch_service(conn, secret_key, "Sonarr", build_sonarr_from_db, fetch_sonarr_queue, result)
    return result


def _fetch_service(
    conn: sqlite3.Connection,
    secret_key: str,
    name: str,
    build: Callable[[sqlite3.Connection, str], ArrClient | None],
    fetch: Callable[..., list[ArrCard]],
    result: FetchResult,
) -> None:
    """Add one service's cards to *result*, or the error that kept them out."""
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
        result.errors.append(f"{name} fetch failed: {exc}")
        return
    stamp_searching_since(conn, name.lower(), cards, complete=not failures)
    result.cards.extend(cards)


def fetch_arr_queue(conn: sqlite3.Connection, secret_key: str) -> list[ArrCard]:
    """Fetch Radarr/Sonarr queues, grouping Sonarr episodes by series.

    Returns a list of download cards.  Movies are one card each.
    TV series are grouped into a single card with an ``episodes`` list.

    This is the wrapper around ``fetch_arr_queue_result`` that drops its error report.
    Callers that need to surface fetch errors to the UI should use that function
    instead.
    """
    return fetch_arr_queue_result(conn, secret_key).cards
