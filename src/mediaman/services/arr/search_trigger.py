"""Throttle state, persistence, and trigger-decision logic for auto-triggered Arr searches.

This module is the single home for everything that governs whether and when
mediaman pokes Radarr/Sonarr to search for a stalled monitored item.

What this module owns:

* Module-level in-memory state dicts and the lock guarding them
  (sourced from :mod:`mediaman.services.arr._throttle_state`).
* Per-item and per-arr-instance backoff configuration and helpers.
* SQLite persistence to/from the ``arr_search_throttle`` table
  (sourced from :mod:`mediaman.services.arr._throttle_persistence`).
* The reconciliation pass that reaps stranded rows after a TTL.
* Inspection / reset helpers used by the UI and tests.
* :func:`maybe_trigger_search` — the reservation-token locking discipline
  and the per-item throttle gate.
* :func:`trigger_pending_searches` — the scheduler-driven sweep.
* :func:`_trigger_sonarr_partial_missing` — the second pass for series with
  partial episode coverage.

State and persistence helpers are re-exported here at module level so that
``mediaman.services.arr.search_trigger.<helper>`` remains a stable patch
target for tests; production calls in this file resolve them as bare names.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, cast

import requests

if TYPE_CHECKING:
    from mediaman.services.arr.fetcher import ArrCard

from mediaman.services.arr._throttle_persistence import (  # noqa: F401
    _STRANDED_THROTTLE_TTL_SECONDS,
    _load_throttle_from_db,
    _save_trigger_to_db,
    clear_throttle,
    get_search_info,
    reconcile_stranded_throttle,
    reset_search_triggers,
)
from mediaman.services.arr._throttle_state import (  # noqa: F401
    _SEARCH_BACKOFF,
    _SEARCH_BACKOFF_BASE_SECONDS,
    _SEARCH_BACKOFF_JITTER,
    _SEARCH_BACKOFF_MAX_SECONDS,
    _arr_throttle_key,
    _last_search_trigger,
    _last_search_trigger_by_arr,
    _reservation_tokens,
    _search_backoff_seconds,
    _search_count,
    _state_lock,
)
from mediaman.services.arr.auto_abandon import maybe_auto_abandon
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.fetcher import fetch_arr_queue
from mediaman.services.infra import ConfigDecryptError, SafeHTTPError

logger = logging.getLogger(__name__)


# ---- Backoff configuration (non-backoff constants kept here) ----

_SEARCH_STALE_SECONDS = 5 * 60  # trigger if item has been searching > 5 min

# Per-arr-instance serialiser: don't fire more than one search per Radarr/Sonarr
# instance within this window, regardless of how many items are eligible. Caps
# fan-out to the indexer when many stuck items contend at the same moment.
_PER_ARR_THROTTLE_SECONDS = 15 * 60


# ---- Public API: trigger decision ----


# rationale: throttle check, Arr client construction, search dispatch, and
# throttle-row write form an atomic guard — splitting the throttle read from
# the search call would open a TOCTOU window where concurrent callers could
# both pass the throttle check and both fire duplicate searches.
def maybe_trigger_search(
    conn,
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

    Locking discipline:

    * The DB read is performed BEFORE acquiring the lock so that a slow
      SQLite query for one dl_id cannot serialise every other dl_id's
      throttle check across all workers — one blocked read would otherwise
      starve sibling workers' in-memory throttle lookups for entirely
      unrelated items.
    * Only the in-memory snapshot work — reading the cache, deciding
      whether the throttle window has expired, and reserving the slot
      — runs under the lock.
    * The Radarr/Sonarr HTTP call runs entirely outside the lock so a
      slow upstream cannot starve other workers' throttle reads.
    * Each attempt registers a unique reservation token; rollback on
      failure compares against that token instead of float-equality on
      the timestamp, so a sibling worker overwriting the slot can no
      longer silently no-op the rollback — the token check distinguishes
      our own attempt from a sibling's fresh reservation that landed
      between our phase-1 reserve and phase-3 rollback.
    * After the network call we re-acquire the lock to update memory,
      and either persist the success or roll back the reservation on
      failure.
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

    # Phase 0: warm-up DB read — done OUTSIDE the lock so a slow SQLite
    # query for one dl_id can't serialise every other dl_id's throttle
    # check across all workers.
    persisted_epoch, persisted_count = _load_throttle_from_db(conn, dl_id)

    # Phase 1: reserve the slot under the lock. Treat *now* as the
    # speculative trigger timestamp so concurrent siblings see the slot
    # taken; we'll roll this back if the network call ultimately fails.
    # ``my_token`` uniquely identifies this attempt so rollback can
    # detect a sibling thread having overwritten the reservation in the
    # meantime — comparing the token rather than float-equality on the
    # timestamp means a sibling's newer reservation is never silently
    # nuked by our rollback.
    my_token = uuid.uuid4().hex
    service = "radarr" if kind == "movie" else "sonarr"
    arr_throttle_key = _arr_throttle_key(service, arr_id)
    with _state_lock:
        last = _last_search_trigger.get(dl_id, 0.0)
        previous_count = _search_count.get(dl_id, 0)
        if last == 0.0 and persisted_epoch > 0.0:
            # Not in memory but the DB says we've poked this dl_id
            # before — warm the cache. Another worker that beat us to
            # the lock may already have a fresher in-memory value, in
            # which case we honour theirs.
            last = persisted_epoch
            _last_search_trigger[dl_id] = persisted_epoch
        if persisted_count > previous_count:
            # Restore count so the "Searched N×" UI hint doesn't reset
            # every time the process restarts.
            _search_count[dl_id] = persisted_count
            previous_count = persisted_count
        if now - last < _search_backoff_seconds(previous_count, dl_id, last):
            return
        # Reserve: bump the in-memory marker and stamp our token so a
        # sibling worker sees this slot as recently triggered. If the
        # network call fails we roll this back to *prev_last* in the
        # finally block below — but only if the token still matches.
        prev_last = last
        _last_search_trigger[dl_id] = now
        _reservation_tokens[dl_id] = my_token

    # Phase 2: outside the lock, do the network call.
    triggered = False
    try:
        builders = {"radarr": build_radarr_from_db, "sonarr": build_sonarr_from_db}
        client = builders[service](conn, secret_key)
        if client is None:
            return
        from mediaman.services.arr.base import ArrClient

        if kind == "movie":
            cast(ArrClient, client).search_movie(arr_id)
        else:
            cast(ArrClient, client).search_series(arr_id)
        triggered = True
        # Late import: ``_deep_links`` lives in the ``download_queue`` package
        # whose ``__init__`` imports ``maybe_trigger_search`` from this module,
        # so a top-level import here would cycle. The post-fire log mirrors
        # the count update done inside the finally block (line below); keeping
        # the value inline avoids shadowing the ``new_count: int | None``
        # declaration the lock-protected commit path relies on.
        from mediaman.services.downloads.download_queue.classify import (
            _format_next_attempt,
        )

        logger.info(
            "Triggered search for stalled item %s (n=%d, %s)",
            dl_id,
            previous_count + 1,
            _format_next_attempt(_search_backoff_seconds(previous_count + 1, dl_id, now)),
        )
    except (
        SafeHTTPError,
        requests.RequestException,
        ArrError,
        sqlite3.Error,
        ConfigDecryptError,
    ):
        logger.warning("Failed to trigger search for %s", dl_id, exc_info=True)
    finally:
        # Phase 3: re-acquire the lock to commit or roll back. The token
        # check guards against a sibling thread that overwrote our
        # reservation between phases 1 and 3 — without it, the prior
        # float-equality on ``now`` could either silently no-op our
        # rollback (if the sibling stamped a newer ``now``) or drop the
        # sibling's reservation. With the token, we either restore our
        # prior value or quietly defer to whoever beat us to the slot.
        new_count: int | None
        with _state_lock:
            if triggered:
                new_count = previous_count + 1
                _search_count[dl_id] = new_count
                # Mirror the timestamp under the arr-id-stable key so a
                # subsequent rename of the same series can't bypass the
                # throttle by producing a fresh title-derived dl_id.
                _last_search_trigger_by_arr[arr_throttle_key] = now
                # Successful trigger keeps the reservation timestamp;
                # the token is no longer load-bearing so drop it to
                # avoid leaking memory.
                if _reservation_tokens.get(dl_id) == my_token:
                    _reservation_tokens.pop(dl_id, None)
            else:
                # Roll back the reservation only if it's still ours.
                if _reservation_tokens.get(dl_id) == my_token:
                    if prev_last > 0.0:
                        _last_search_trigger[dl_id] = prev_last
                    else:
                        _last_search_trigger.pop(dl_id, None)
                    _reservation_tokens.pop(dl_id, None)
                new_count = None  # signals: do not persist

        # The DB write is also outside the throttle lock — SQLite
        # handles its own concurrency and we don't want to block sibling
        # workers' throttle reads on a slow disk fsync.
        if triggered and new_count is not None:
            _save_trigger_to_db(conn, dl_id, now, new_count)


def trigger_pending_searches(conn, secret_key: str) -> None:
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
    except (
        SafeHTTPError,
        requests.RequestException,
        ArrError,
        sqlite3.Error,
        ConfigDecryptError,
    ):
        logger.warning("trigger_pending_searches: failed to fetch arr queue", exc_info=True)
        arr_items = []

    now = time.time()
    for item in arr_items:
        item_dict = cast(dict, item)
        maybe_trigger_search(conn, item_dict, matched_nzb=False, secret_key=secret_key)
        try:
            maybe_auto_abandon(conn, secret_key, item=item_dict, now=now)
        except Exception:  # rationale: §6.4 site 2 — scheduler must survive a single bad row
            logger.warning(
                "auto-abandon: skipped %s due to error", item.get("dl_id"), exc_info=True
            )

    try:
        _trigger_sonarr_partial_missing(conn, arr_items, secret_key)
    except (
        SafeHTTPError,
        requests.RequestException,
        ArrError,
        sqlite3.Error,
        ConfigDecryptError,
    ):
        logger.warning(
            "trigger_pending_searches: sonarr partial-missing pass failed",
            exc_info=True,
        )


def _trigger_sonarr_partial_missing(
    conn,
    arr_items: list[ArrCard],
    secret_key: str,
) -> None:
    """Fire SeriesSearch for Sonarr series with partial missing episodes.

    Dedupes against series already handled by the main pass via
    ``arr_id``, and reuses the ``sonarr:{title}`` dl_id format so the
    per-item throttle recognises the same series across passes.
    """
    from mediaman.services.arr.base import ArrClient

    client = build_sonarr_from_db(conn, secret_key)
    if client is None:
        return
    # ``build_sonarr_from_db`` always yields an ArrClient with SONARR_SPEC; narrow for mypy.
    sonarr_client = cast(ArrClient, client)

    already_poked = {
        item.get("arr_id")
        for item in arr_items
        if item.get("kind") == "series" and item.get("arr_id")
    }

    # The per-``dl_id`` throttle in ``maybe_trigger_search`` collapses
    # under a Sonarr title rename — a renamed series produces a fresh
    # ``sonarr:{title}`` key on every tick, bypassing the throttle.
    # Pre-filter on the arr-id-stable parallel throttle so a renamed
    # series we recently triggered cannot be re-poked here.
    now = time.time()
    missing = sonarr_client.get_missing_series()
    for series_id, title in missing.items():
        if series_id in already_poked:
            continue
        with _state_lock:
            arr_last = _last_search_trigger_by_arr.get(_arr_throttle_key("sonarr", series_id), 0.0)
        if now - arr_last < _PER_ARR_THROTTLE_SECONDS:
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
