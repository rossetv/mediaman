"""Auto-trigger Radarr/Sonarr searches for stalled monitored items.

Owns the module-level throttle state and the background scheduler job
:func:`trigger_pending_searches`. The per-request trigger function
``_maybe_trigger_search`` lives in :mod:`mediaman.services.download_queue`
because tests patch it there.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

logger = logging.getLogger("mediaman")

# Module-level throttle for auto-triggered Radarr/Sonarr searches.
# Maps dl_id -> epoch seconds of last trigger.
_last_search_trigger: dict[str, float] = {}

# Parallel map: dl_id -> number of times we've triggered a search for this
# item since process start. Powers the "Searched N times" UI hint so users
# can see mediaman is actually poking Radarr/Sonarr rather than idling.
_search_count: dict[str, int] = {}

# Lock guarding _last_search_trigger and _search_count.
_state_lock = threading.Lock()

_SEARCH_STALE_SECONDS = 5 * 60     # trigger if item has been searching > 5 min
_SEARCH_THROTTLE_SECONDS = 15 * 60  # don't re-trigger within 15 min


def _reset_search_triggers() -> None:
    """Clear the in-memory search-trigger snapshot. Used by tests."""
    _last_search_trigger.clear()
    _search_count.clear()


def _get_search_info(dl_id: str) -> tuple[int, float]:
    """Return ``(count, last_epoch_seconds)`` for a dl_id.

    ``(0, 0.0)`` means mediaman has never fired a search for this item
    (e.g. it's still within the 5-min staleness window, or the process
    was restarted since). Callers render this as "Added Xm ago" using
    the item's own added_at, rather than a misleading "Never searched".
    """
    with _state_lock:
        return _search_count.get(dl_id, 0), _last_search_trigger.get(dl_id, 0.0)


def trigger_pending_searches(conn: sqlite3.Connection) -> None:
    """Poke Radarr/Sonarr to search for every monitored-but-missing item.

    Called from the APScheduler on a fixed interval so items don't sit in
    "searching" indefinitely when nobody's got the /downloads page open.

    Two passes:

    1. Iterate everything :func:`fetch_arr_queue` surfaces — covers every
       Radarr movie with no file, and every Sonarr series with zero
       episode files.
    2. Hit Sonarr's ``wanted/missing`` endpoint to catch series that
       already have *some* episodes and are missing others — these are
       filtered out of pass 1 by the ``episodeFileCount > 0`` guard in
       :func:`fetch_arr_queue`.

    Reuses the per-item throttle and ``arr_id == 0`` gate inside
    :func:`_maybe_trigger_search` (from :mod:`mediaman.services.download_queue`),
    so already-queued items and recently-searched items are skipped automatically.
    """
    # Import here to avoid circular dependency: download_queue imports from
    # arr_search_trigger, so arr_search_trigger must not import from
    # download_queue at module level.
    from mediaman.services.download_queue import (
        _build_arr_client,
        _maybe_trigger_search,
        _get_arr_queue,
    )

    try:
        arr_items = _get_arr_queue(conn)
    except Exception:
        logger.warning("trigger_pending_searches: failed to fetch arr queue", exc_info=True)
        arr_items = []

    for item in arr_items:
        _maybe_trigger_search(conn, item, matched_nzb=False)

    try:
        _trigger_sonarr_partial_missing(conn, arr_items)
    except Exception:
        logger.warning(
            "trigger_pending_searches: sonarr partial-missing pass failed",
            exc_info=True,
        )


def _trigger_sonarr_partial_missing(
    conn: sqlite3.Connection, arr_items: list[dict]
) -> None:
    """Fire SeriesSearch for Sonarr series with partial missing episodes.

    Dedupes against series already handled by the main pass via
    ``arr_id``, and reuses the ``sonarr:{title}`` dl_id format so the
    per-item throttle recognises the same series across passes.
    """
    from mediaman.services.download_queue import _build_arr_client, _maybe_trigger_search

    client = _build_arr_client(conn, "sonarr")
    if client is None:
        return

    already_poked = {
        item.get("arr_id")
        for item in arr_items
        if item.get("kind") == "series" and item.get("arr_id")
    }

    missing = client.get_missing_series()
    for series_id, title in missing.items():
        if series_id in already_poked:
            continue
        _maybe_trigger_search(
            conn,
            {
                "kind": "series",
                "dl_id": f"sonarr:{title}",
                "arr_id": series_id,
                "is_upcoming": False,
                "added_at": 0.0,
            },
            matched_nzb=False,
        )
