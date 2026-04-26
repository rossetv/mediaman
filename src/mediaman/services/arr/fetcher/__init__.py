"""arr_fetcher package — Radarr/Sonarr queue fetch.

Public entry points:

- :func:`fetch_arr_queue` -- backward-compatible, returns a plain list of cards.
- :func:`fetch_arr_queue_result` -- returns a :class:`FetchResult` that also
  carries any fetch errors so the UI can display a banner.

All previously-public symbols are re-exported here so existing imports such as
``from mediaman.services.arr.fetcher import FetchResult, fetch_arr_queue``
continue to work without modification.
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.services.arr.fetcher._base import (
    ArrCard,
    ArrEpisodeEntry,
    BaseArrCard,
    FetchResult,
)
from mediaman.services.arr.fetcher._radarr import fetch_radarr_queue
from mediaman.services.arr.fetcher._sonarr import fetch_sonarr_queue

logger = logging.getLogger("mediaman")

__all__ = [
    "ArrCard",
    "ArrEpisodeEntry",
    "BaseArrCard",
    "FetchResult",
    "fetch_arr_queue",
    "fetch_arr_queue_result",
]


def fetch_arr_queue_result(conn: sqlite3.Connection, secret_key: str) -> FetchResult:
    """Fetch Radarr/Sonarr queues and return a :class:`FetchResult`.

    ``secret_key`` is required to decrypt the Radarr/Sonarr API keys stored
    in DB settings.

    Unlike :func:`fetch_arr_queue`, this surfaces errors alongside cards so
    callers can show a UI banner when a service is unreachable.
    """
    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db

    result = FetchResult()

    try:
        radarr_client = build_radarr_from_db(conn, secret_key)
        if radarr_client is not None:
            result.cards.extend(fetch_radarr_queue(radarr_client))
    except Exception as exc:
        msg = f"Radarr fetch failed: {exc}"
        logger.warning("Failed to fetch Radarr queue: %s", exc, exc_info=True)
        result.errors.append(msg)

    try:
        sonarr_client = build_sonarr_from_db(conn, secret_key)
        if sonarr_client is not None:
            result.cards.extend(fetch_sonarr_queue(sonarr_client))
    except Exception as exc:
        msg = f"Sonarr fetch failed: {exc}"
        logger.warning("Failed to fetch Sonarr queue: %s", exc, exc_info=True)
        result.errors.append(msg)

    return result


def fetch_arr_queue(conn: sqlite3.Connection, secret_key: str) -> list[ArrCard]:
    """Fetch Radarr/Sonarr queues, grouping Sonarr episodes by series.

    Returns a list of download cards.  Movies are one card each.
    TV series are grouped into a single card with an ``episodes`` list.

    This is the backward-compatible wrapper around :func:`fetch_arr_queue_result`.
    Callers that need to surface fetch errors to the UI should use that function
    instead.
    """
    return fetch_arr_queue_result(conn, secret_key).cards
