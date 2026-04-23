"""Auto-trigger Radarr/Sonarr searches for stalled monitored items.

Owns the module-level throttle state, :func:`maybe_trigger_search`, and
the background scheduler job :func:`trigger_pending_searches`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from mediaman.services.arr_build import build_arr_client
from mediaman.services.arr_fetcher import fetch_arr_queue

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


def reset_search_triggers() -> None:
    """Clear the in-memory search-trigger snapshot. Used by tests."""
    _last_search_trigger.clear()
    _search_count.clear()

def _load_last_trigger_from_db(conn: sqlite3.Connection, dl_id: str) -> float:
    """Return the persisted last-triggered epoch for *dl_id*, or 0.0.

    Reads from the ``arr_search_throttle`` table.  Returns 0.0 when the
    table doesn't exist yet (pre-migration DBs during startup).
    """
    try:
        row = conn.execute(
            "SELECT last_triggered_at FROM arr_search_throttle WHERE key=?",
            (dl_id,),
        ).fetchone()
        if row is None:
            return 0.0
        from mediaman.services.format import parse_iso_utc
        dt = parse_iso_utc(row[0])
        return dt.timestamp() if dt is not None else 0.0
    except Exception:
        return 0.0


def _save_trigger_to_db(conn: sqlite3.Connection, dl_id: str, epoch: float) -> None:
    """Persist *epoch* as the last-triggered time for *dl_id*.

    Uses ``INSERT OR REPLACE`` so the upsert is idempotent.  Failures are
    logged and swallowed — the in-memory state is still correct even when
    the write fails (e.g. DB locked briefly).
    """
    try:
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO arr_search_throttle (key, last_triggered_at) VALUES (?, ?)",
            (dl_id, ts),
        )
        conn.commit()
    except Exception:
        logger.warning(
            "arr_search_trigger: failed to persist throttle for %s", dl_id, exc_info=True
        )


def maybe_trigger_search(
    conn: sqlite3.Connection, item: dict, matched_nzb: bool
) -> None:
    """Trigger a Radarr/Sonarr search for a stalled item, with throttling.

    Does nothing when:
    - item is upcoming (Radarr/Sonarr correctly won't search for it)
    - item is matched to an NZBGet entry (actively downloading)
    - item was added less than 5 minutes ago
    - a search was triggered for the same dl_id within the last 15 minutes
    """
    if item.get("is_upcoming"):
        return
    if matched_nzb:
        return
    arr_id = item.get("arr_id") or 0
    if not arr_id:
        return
    added_at = item.get("added_at") or 0.0
    now = time.time()
    if now - added_at < _SEARCH_STALE_SECONDS:
        return

    dl_id = item.get("dl_id") or ""

    with _state_lock:
        last = _last_search_trigger.get(dl_id, 0.0)
        if last == 0.0:
            # Not in memory — check the DB (handles restarts/deploys).
            last = _load_last_trigger_from_db(conn, dl_id)
            if last > 0.0:
                _last_search_trigger[dl_id] = last  # warm the cache
        if now - last < _SEARCH_THROTTLE_SECONDS:
            return

        try:
            if item.get("kind") == "movie":
                client = build_arr_client(conn, "radarr")
                if client is None:
                    return
                client.search_movie(arr_id)
            elif item.get("kind") == "series":
                client = build_arr_client(conn, "sonarr")
                if client is None:
                    return
                client.search_series(arr_id)
            else:
                return
            _last_search_trigger[dl_id] = now
            _search_count[dl_id] = _search_count.get(dl_id, 0) + 1
            logger.info("Triggered search for stalled item %s", dl_id)
            _save_trigger_to_db(conn, dl_id, now)
        except Exception:
            logger.warning(
                "Failed to trigger search for %s", dl_id, exc_info=True
            )


def get_search_info(dl_id: str) -> tuple[int, float]:
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
    :func:`maybe_trigger_search`, so already-queued items and
    recently-searched items are skipped automatically.
    """
    try:
        arr_items = fetch_arr_queue(conn)
    except Exception:
        logger.warning("trigger_pending_searches: failed to fetch arr queue", exc_info=True)
        arr_items = []

    for item in arr_items:
        maybe_trigger_search(conn, item, matched_nzb=False)

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
    client = build_arr_client(conn, "sonarr")
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
        maybe_trigger_search(
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
