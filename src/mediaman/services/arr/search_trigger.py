"""Auto-trigger Radarr/Sonarr searches for stalled monitored items.

Owns the module-level throttle state, :func:`maybe_trigger_search`, and
the background scheduler job :func:`trigger_pending_searches`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from mediaman.services.arr.build import build_arr_client
from mediaman.services.arr.fetcher import fetch_arr_queue
from mediaman.services.infra.settings_reader import get_int_setting

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

_SEARCH_STALE_SECONDS = 5 * 60  # trigger if item has been searching > 5 min
_SEARCH_THROTTLE_SECONDS = 15 * 60  # don't re-trigger within 15 min


def reset_search_triggers() -> None:
    """Clear the in-memory search-trigger snapshot. Used by tests."""
    _last_search_trigger.clear()
    _search_count.clear()


def _load_throttle_from_db(conn: sqlite3.Connection, dl_id: str) -> tuple[float, int]:
    """Return ``(last_triggered_epoch, search_count)`` for *dl_id*.

    Reads from the ``arr_search_throttle`` table.  Returns ``(0.0, 0)``
    when the table or row doesn't exist yet (pre-migration DBs during
    startup, or items mediaman has never poked).
    """
    try:
        row = conn.execute(
            "SELECT last_triggered_at, search_count FROM arr_search_throttle WHERE key=?",
            (dl_id,),
        ).fetchone()
        if row is None:
            return 0.0, 0
        from mediaman.services.infra.format import parse_iso_utc

        dt = parse_iso_utc(row[0])
        epoch = dt.timestamp() if dt is not None else 0.0
        count = int(row[1] or 0)
        return epoch, count
    except Exception:
        return 0.0, 0


def _save_trigger_to_db(conn: sqlite3.Connection, dl_id: str, epoch: float, count: int) -> None:
    """Persist *epoch* and *count* for *dl_id*.

    Uses ``INSERT OR REPLACE`` so the upsert is idempotent.  Failures are
    logged and swallowed — the in-memory state is still correct even when
    the write fails (e.g. DB locked briefly).
    """
    try:
        from datetime import datetime, timezone

        ts = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO arr_search_throttle "
            "(key, last_triggered_at, search_count) VALUES (?, ?, ?)",
            (dl_id, ts, count),
        )
        conn.commit()
    except Exception:
        logger.warning(
            "arr_search_trigger: failed to persist throttle for %s", dl_id, exc_info=True
        )


def maybe_trigger_search(
    conn: sqlite3.Connection,
    item: dict,
    matched_nzb: bool,
    secret_key: str = "",
) -> None:
    """Trigger a Radarr/Sonarr search for a stalled item, with throttling.

    ``secret_key`` is required for decrypting the stored Radarr/Sonarr API
    key. When not supplied (empty string) the search is skipped — this
    preserves backward compatibility for callers that don't yet hold a key.

    Does nothing when:
    - item is upcoming (Radarr/Sonarr correctly won't search for it)
    - item is matched to an NZBGet entry (actively downloading)
    - item was added less than 5 minutes ago
    - a search was triggered for the same dl_id within the last 15 minutes
    - secret_key is empty

    Locking discipline (finding 25): the in-memory throttle lock is held
    only while inspecting and reserving the per-``dl_id`` slot. The
    Radarr/Sonarr HTTP call runs *outside* the lock so a slow upstream
    cannot starve other workers' throttle reads. After the network call
    we re-acquire the lock to update memory, and either persist the
    success or roll back the reservation on failure.
    """
    if item.get("is_upcoming"):
        return
    if matched_nzb:
        return
    if not secret_key:
        return
    arr_id = item.get("arr_id") or 0
    if not arr_id:
        return
    added_at = item.get("added_at") or 0.0
    now = time.time()
    if now - added_at < _SEARCH_STALE_SECONDS:
        return

    dl_id = item.get("dl_id") or ""
    kind = item.get("kind")
    if kind not in ("movie", "series"):
        return

    # Phase 1: reserve the slot under the lock, mirroring the existing
    # cache-then-DB warm-up. Treat *now* as the speculative trigger
    # timestamp so concurrent siblings see the slot taken; we'll roll
    # this back if the network call ultimately fails.
    with _state_lock:
        last = _last_search_trigger.get(dl_id, 0.0)
        previous_count = _search_count.get(dl_id, 0)
        if last == 0.0:
            # Not in memory — check the DB (handles restarts/deploys).
            last, persisted_count = _load_throttle_from_db(conn, dl_id)
            if last > 0.0:
                _last_search_trigger[dl_id] = last  # warm the cache
                # Restore count too so the "Searched N×" UI hint doesn't
                # reset every time the process restarts.
                if persisted_count > _search_count.get(dl_id, 0):
                    _search_count[dl_id] = persisted_count
                    previous_count = persisted_count
        if now - last < _SEARCH_THROTTLE_SECONDS:
            return
        # Reserve: bump the in-memory marker so a sibling worker sees
        # this slot as recently triggered. If the network call fails
        # we roll this back to *last* in the finally block below.
        prev_last = last
        _last_search_trigger[dl_id] = now

    # Phase 2: outside the lock, do the network call.
    success = False
    try:
        service = "radarr" if kind == "movie" else "sonarr"
        client = build_arr_client(conn, service, secret_key)
        if client is None:
            return
        if kind == "movie":
            client.search_movie(arr_id)
        else:
            client.search_series(arr_id)
        success = True
        logger.info("Triggered search for stalled item %s", dl_id)
    except Exception:
        logger.warning("Failed to trigger search for %s", dl_id, exc_info=True)
    finally:
        # Phase 3: re-acquire the lock to commit or roll back.
        with _state_lock:
            if success:
                new_count = previous_count + 1
                _search_count[dl_id] = new_count
            else:
                # Roll back the reservation so a future call can retry
                # rather than waiting out a 15-minute throttle window
                # against a request that never actually fired.
                if _last_search_trigger.get(dl_id) == now:
                    if prev_last > 0.0:
                        _last_search_trigger[dl_id] = prev_last
                    else:
                        _last_search_trigger.pop(dl_id, None)
                new_count = None  # signals: do not persist

        # The DB write is also outside the throttle lock — SQLite
        # handles its own concurrency and we don't want to block sibling
        # workers' throttle reads on a slow disk fsync.
        if success and new_count is not None:
            _save_trigger_to_db(conn, dl_id, now, new_count)


def get_search_info(dl_id: str) -> tuple[int, float]:
    """Return ``(count, last_epoch_seconds)`` for a dl_id.

    Reads the in-memory cache first and falls back to the persisted
    ``arr_search_throttle`` row when the cache is empty for *dl_id*.
    The DB read warms the cache so subsequent calls in this worker stay
    in-memory.

    Falling back to the DB is essential under multi-worker deployments:
    only the worker that fires the search bumps its own in-memory
    counter, but the persisted row is shared. Without the fallback, the
    Downloads page flickers between "Searched N×" and "Added X days
    ago, waiting for first search" as poll requests bounce across
    workers — same flicker happens to a single worker after a restart
    until it next fires a search.

    ``(0, 0.0)`` is only returned when nothing is in memory AND nothing
    is in the DB.
    """
    with _state_lock:
        count = _search_count.get(dl_id, 0)
        last = _last_search_trigger.get(dl_id, 0.0)
        if count > 0 or last > 0:
            return count, last

    # Cache miss — consult the DB. Best-effort: any failure (locked DB,
    # missing table on a fresh install) returns the zero pair rather
    # than raising, so a stalled connection never breaks the page.
    try:
        from mediaman.db import get_db

        epoch, persisted_count = _load_throttle_from_db(get_db(), dl_id)
    except Exception:
        return 0, 0.0
    if epoch == 0.0 and persisted_count == 0:
        return 0, 0.0

    with _state_lock:
        # Warm the cache, but never let a DB read clobber a higher
        # in-memory count (which means this worker has fired a search
        # since the row was last persisted).
        if dl_id not in _last_search_trigger:
            _last_search_trigger[dl_id] = epoch
        if persisted_count > _search_count.get(dl_id, 0):
            _search_count[dl_id] = persisted_count
        return (
            _search_count.get(dl_id, 0),
            _last_search_trigger.get(dl_id, 0.0),
        )


def clear_throttle(conn: sqlite3.Connection, dl_id: str) -> None:
    """Forget every trace of *dl_id* from the search-throttle subsystem.

    Removes the persisted ``arr_search_throttle`` row and drops the entry
    from both in-memory caches.  Used by the abandon flow so a future
    re-monitor starts a fresh search-count rather than inheriting a stale
    "Searched 52×" hint.

    Idempotent: calling on a key that was never seen is a no-op.
    """
    with _state_lock:
        _last_search_trigger.pop(dl_id, None)
        _search_count.pop(dl_id, None)
    try:
        conn.execute("DELETE FROM arr_search_throttle WHERE key=?", (dl_id,))
        conn.commit()
    except Exception:
        logger.warning("arr_search_trigger: failed to clear throttle for %s", dl_id, exc_info=True)


def maybe_auto_abandon(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    item: dict,
    search_count: int,
) -> None:
    """Auto-unmonitor *item* if its search count has crossed the threshold.

    Multiplier of 0 (default) disables the feature; the function returns
    immediately. Otherwise abandons via the same service entry-points the
    manual button uses, so semantics (throttle clear, partial-failure
    behaviour, logging) are identical.

    Series with no derivable season list (no episodes in the queue) are
    skipped — there's nothing for Sonarr to unmonitor that wouldn't be a
    no-op or an error.
    """
    multiplier = get_int_setting(conn, "abandon_search_auto_multiplier", default=0, min=0, max=100)
    if multiplier <= 0:
        return
    escalate_at = get_int_setting(conn, "abandon_search_escalate_at", default=50, min=2, max=10000)
    if search_count < escalate_at * multiplier:
        return

    # Late import breaks the otherwise-circular dependency between
    # search_trigger and the abandon service (which itself imports
    # clear_throttle from this module).
    from mediaman.services.downloads.abandon import (
        abandon_movie,
        abandon_seasons,
    )

    dl_id = item.get("dl_id") or ""
    arr_id = item.get("arr_id") or 0
    if not dl_id or not arr_id:
        return

    if item.get("kind") == "movie":
        abandon_movie(conn, secret_key, arr_id=arr_id, dl_id=dl_id)
        return

    seasons = sorted({int(ep.get("season_number") or 0) for ep in (item.get("episodes") or [])})
    if not seasons:
        return
    abandon_seasons(
        conn,
        secret_key,
        series_id=arr_id,
        season_numbers=seasons,
        dl_id=dl_id,
    )


def trigger_pending_searches(conn: sqlite3.Connection, secret_key: str) -> None:
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

    Args:
        conn: Open SQLite connection with ``row_factory`` set to
            :class:`sqlite3.Row`.
        secret_key: Application secret used to decrypt stored Radarr/Sonarr
            API keys.
    """
    try:
        arr_items = fetch_arr_queue(conn, secret_key)
    except Exception:
        logger.warning("trigger_pending_searches: failed to fetch arr queue", exc_info=True)
        arr_items = []

    for item in arr_items:
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key=secret_key)
        try:
            count, _ = get_search_info(item.get("dl_id") or "")
            maybe_auto_abandon(conn, secret_key, item=item, search_count=count)
        except Exception:
            logger.warning(
                "auto-abandon: skipped %s due to error", item.get("dl_id"), exc_info=True
            )

    try:
        _trigger_sonarr_partial_missing(conn, arr_items, secret_key)
    except Exception:
        logger.warning(
            "trigger_pending_searches: sonarr partial-missing pass failed",
            exc_info=True,
        )


def _trigger_sonarr_partial_missing(
    conn: sqlite3.Connection,
    arr_items: list[dict],
    secret_key: str,
) -> None:
    """Fire SeriesSearch for Sonarr series with partial missing episodes.

    Dedupes against series already handled by the main pass via
    ``arr_id``, and reuses the ``sonarr:{title}`` dl_id format so the
    per-item throttle recognises the same series across passes.
    """
    client = build_arr_client(conn, "sonarr", secret_key)
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
            secret_key=secret_key,
        )
