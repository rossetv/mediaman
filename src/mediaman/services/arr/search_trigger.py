"""Throttle state, persistence, and trigger-decision logic for auto-triggered Arr searches.

This module is the single home for everything that governs whether and when
mediaman pokes Radarr/Sonarr to search for a stalled monitored item.  It was
previously split across :mod:`mediaman.services.arr.throttle` (state and
persistence) and this file (decision logic), with a messy re-export dance to
keep callers and tests happy.  The split is now gone; everything lives here.

What this module owns:

* Module-level in-memory state dicts and the lock guarding them.
* Per-item and per-arr-instance backoff configuration and helpers.
* SQLite persistence to/from the ``arr_search_throttle`` table.
* The reconciliation pass that reaps stranded rows after a TTL.
* Inspection / reset helpers used by the UI and tests.
* :func:`maybe_trigger_search` — the reservation-token locking discipline
  and the per-item throttle gate.
* :func:`trigger_pending_searches` — the scheduler-driven sweep.
* :func:`_trigger_sonarr_partial_missing` — the second pass for series with
  partial episode coverage.

:mod:`mediaman.services.arr.throttle` is kept as a back-compat re-export
shim so callers and tests that import from that path continue to work.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from datetime import UTC
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from mediaman.services.arr.fetcher import ArrCard

from mediaman.services.arr.auto_abandon import maybe_auto_abandon
from mediaman.services.arr.build import build_arr_client
from mediaman.services.arr.fetcher import fetch_arr_queue
from mediaman.services.infra.backoff import ExponentialBackoff

logger = logging.getLogger("mediaman")


# ---- Persisted/in-memory throttle state ----

# Maps dl_id -> epoch seconds of last trigger.
_last_search_trigger: dict[str, float] = {}

# Parallel map: dl_id -> number of times we've triggered a search for this
# item since process start. Powers the "Searched N times" UI hint so users
# can see mediaman is actually poking Radarr/Sonarr rather than idling.
_search_count: dict[str, int] = {}

# Tokens identifying the current owner of each dl_id's reservation. The
# token is generated under the lock when a worker reserves the slot and
# checked again on rollback so a sibling worker overwriting the slot in
# the meantime cannot have its work undone (Domain-06 #8).
_reservation_tokens: dict[str, str] = {}

# Stable composite-key throttle indexed by ``f"{service}:#{arr_id}"``.
# The ``dl_id``-based throttle in ``_last_search_trigger`` collapses
# under a Sonarr/Radarr title rename (the title-derived dl_id changes),
# but ``arr_id`` is stable. ``maybe_trigger_search`` mirrors every
# successful trigger here so a renamed series can't bypass the throttle
# by producing a fresh title-derived dl_id (Domain-06 #11).
_last_search_trigger_by_arr: dict[str, float] = {}

# Lock guarding _last_search_trigger, _search_count, _reservation_tokens,
# and _last_search_trigger_by_arr.
_state_lock = threading.Lock()


# ---- Backoff configuration ----

_SEARCH_STALE_SECONDS = 5 * 60  # trigger if item has been searching > 5 min

# Per-arr-instance serialiser: don't fire more than one search per Radarr/Sonarr
# instance within this window, regardless of how many items are eligible. Caps
# fan-out to the indexer when many stuck items contend at the same moment.
_PER_ARR_THROTTLE_SECONDS = 15 * 60

# Per-item exponential backoff. interval(n) = base * 2^max(n-1, 0), clamped.
# n is the number of fires already completed for this dl_id; the gate uses it
# to compute the wait until the next allowed fire.
_SEARCH_BACKOFF_BASE_SECONDS = 120  # 2 min
_SEARCH_BACKOFF_MAX_SECONDS = 86_400  # 24 h cap
_SEARCH_BACKOFF_JITTER = 0.1  # ±10% multiplicative jitter

_SEARCH_BACKOFF = ExponentialBackoff(
    _SEARCH_BACKOFF_BASE_SECONDS,
    _SEARCH_BACKOFF_MAX_SECONDS,
    jitter=_SEARCH_BACKOFF_JITTER,
)

_STRANDED_THROTTLE_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days


# ---- Internal helpers ----


def _jitter_for(dl_id: str, last_triggered_at: float) -> float:
    """Return the deterministic ±10% jitter multiplier for *(dl_id, last_triggered_at)*.

    Kept as a thin shim so existing tests can ``monkeypatch`` it to
    pin the multiplier to a constant when asserting on the unjittered
    backoff curve.  Production code routes through ``_SEARCH_BACKOFF.delay``,
    which calls into :class:`~mediaman.services.infra.backoff.ExponentialBackoff`'s
    deterministic-multiplier helper using the same seed.

    Tests may patch either ``mediaman.services.arr.throttle._jitter_for`` or
    ``mediaman.services.arr.search_trigger._jitter_for``.  ``_search_backoff_seconds``
    resolves this name via the ``throttle`` module at call time (lazy import)
    so that patching either path affects the backoff computation.
    """
    seed = f"{dl_id}|{last_triggered_at!r}".encode()
    return _SEARCH_BACKOFF._deterministic_multiplier(seed)


def _search_backoff_seconds(search_count: int, dl_id: str, last_triggered_at: float) -> float:
    """Return the wait in seconds before the next fire is allowed.

    *search_count* is the number of fires already completed for *dl_id*.
    The result is the jittered interval that gates the next fire after
    *last_triggered_at*.

    The seed encodes ``(dl_id, last_triggered_at)`` — the ``!r`` formatting
    of the float is deliberate and part of the determinism contract: it
    produces a consistent representation across platforms and Python versions,
    unlike bare ``str(float)``.  See :class:`~mediaman.services.infra.backoff.ExponentialBackoff`
    for why determinism is load-bearing here.

    Routes the multiplier through the ``throttle`` module's ``_jitter_for``
    attribute (lazy import) so that test monkeypatches on
    ``mediaman.services.arr.throttle._jitter_for`` continue to override
    the multiplier as expected.
    """
    import mediaman.services.arr.throttle as _throttle

    n = max(search_count, 0)
    base = min(_SEARCH_BACKOFF_BASE_SECONDS * 2 ** max(n - 1, 0), _SEARCH_BACKOFF_MAX_SECONDS)
    return min(base * _throttle._jitter_for(dl_id, last_triggered_at), _SEARCH_BACKOFF_MAX_SECONDS)


def _arr_throttle_key(service: str, arr_id: int) -> str:
    """Return the stable arr-id-based throttle key.

    Used as a parallel index to ``_last_search_trigger`` (which is keyed
    by ``dl_id``). The ``dl_id`` collapses under a title rename;
    the arr-id key does not. ``maybe_trigger_search`` updates both, so
    any path that has access to ``(service, arr_id)`` can dedupe even
    if the title has changed since the last trigger (Domain-06 #11).
    """
    return f"{service}:#{arr_id}"


# ---- Persistence ----


def _load_throttle_from_db(conn: sqlite3.Connection, dl_id: str) -> tuple[float, int]:
    """Return ``(last_triggered_epoch, search_count)`` for *dl_id*.

    Reads from the ``arr_search_throttle`` table.  Returns ``(0.0, 0)``
    when the table or row doesn't exist yet (pre-migration DBs during
    startup, or items mediaman has never poked).

    Exception policy (Domain-06 #9): only ``sqlite3.OperationalError``
    and ``sqlite3.DatabaseError`` are swallowed — those genuinely
    represent transient or pre-migration states where ``(0.0, 0)`` is
    the correct fallback. A broader ``except Exception`` previously
    masked schema/migration faults too, silently disabling the
    throttle by reporting "never triggered" for every dl_id.  Any other
    exception (e.g. a coding bug in the parser) now propagates so the
    caller sees the real failure.
    """
    try:
        row = conn.execute(
            "SELECT last_triggered_at, search_count FROM arr_search_throttle WHERE key=?",
            (dl_id,),
        ).fetchone()
        if row is None:
            return 0.0, 0
        from mediaman.services.infra.time import parse_iso_utc

        dt = parse_iso_utc(row[0])
        epoch = dt.timestamp() if dt is not None else 0.0
        count = int(row[1] or 0)
        return epoch, count
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        # Transient or pre-migration state — fall back to "never
        # triggered" so the throttle warm-up doesn't fail loudly the
        # first time a fresh DB is brought up.
        logger.warning(
            "arr_search_trigger: throttle load fell back to zeros for %s: %s",
            dl_id,
            exc,
        )
        return 0.0, 0


def _save_trigger_to_db(conn: sqlite3.Connection, dl_id: str, epoch: float, count: int) -> None:
    """Persist *epoch* and *count* for *dl_id*.

    Uses ``INSERT OR REPLACE`` so the upsert is idempotent.  Failures are
    logged and swallowed — the in-memory state is still correct even when
    the write fails (e.g. DB locked briefly).
    """
    try:
        from datetime import datetime

        ts = datetime.fromtimestamp(epoch, tz=UTC).isoformat()
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

    Connection caveat (Domain-06 #13): the DB-fallback branch calls
    ``mediaman.db.get_db()`` rather than reusing the connection passed
    in by the caller — because this entry point takes only a ``dl_id``
    string and doesn't have a connection in scope. ``get_db()`` returns
    the request-local connection from the FastAPI middleware, which is
    a DIFFERENT SQLite connection from the one the background scheduler
    thread holds when it persists throttle writes. SQLite's WAL mode
    handles cross-connection visibility, so committed writes from the
    scheduler are immediately visible here, but the two connections
    are not the same — anyone modifying this code should be aware that
    transactions started by the caller are NOT in scope here.
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


def reconcile_stranded_throttle(
    conn: sqlite3.Connection,
    *,
    ttl_seconds: int = _STRANDED_THROTTLE_TTL_SECONDS,
) -> int:
    """Delete ``arr_search_throttle`` rows older than *ttl_seconds*.

    Domain-06 #10. Rows in ``arr_search_throttle`` accumulate forever
    when an item is deleted from Radarr/Sonarr — nothing else
    references the row, but ``clear_throttle`` is only called by the
    abandon flow. Operators who delete items directly via the
    Radarr/Sonarr UI never trip that path, so the table grows
    monotonically.

    The reconciliation rule is age-based: if a row hasn't been touched
    in ``ttl_seconds`` (default 90 days), the item is either deleted
    or so deeply forgotten that resetting its search-count is the
    desired behaviour.  Active items are re-triggered well inside the
    15-minute throttle window, so their ``last_triggered_at`` updates
    constantly and they're never reaped.

    Designed to be called once on startup; a stalled DB returns 0
    rather than raising so a slow disk doesn't break boot.

    Args:
        conn: Open SQLite connection.
        ttl_seconds: Cutoff age in seconds. Rows whose
            ``last_triggered_at`` is older than ``now - ttl_seconds``
            are deleted.

    Returns:
        Number of rows deleted.
    """
    from datetime import datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(seconds=ttl_seconds)).isoformat()
    try:
        cur = conn.execute(
            "DELETE FROM arr_search_throttle WHERE last_triggered_at < ?",
            (cutoff,),
        )
        conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        logger.warning(
            "arr_search_trigger: failed to reconcile stranded throttle rows",
            exc_info=True,
        )
        return 0

    deleted = cur.rowcount or 0
    if deleted:
        logger.info(
            "arr_search_trigger.reconcile deleted=%d cutoff=%s",
            deleted,
            cutoff,
        )
    return deleted


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


def reset_search_triggers() -> None:
    """Clear the in-memory search-trigger snapshot. Used by tests."""
    _last_search_trigger.clear()
    _search_count.clear()
    _reservation_tokens.clear()
    _last_search_trigger_by_arr.clear()


# ---- Public API: trigger decision ----


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

    Locking discipline (findings 25 + Domain-06 #7, #8):

    * The DB read is performed BEFORE acquiring the lock. A locked
      SQLite database otherwise blocks every sibling worker's throttle
      check across all dl_ids, since one slow read for any single
      dl_id serialises the lot.
    * Only the in-memory snapshot work — reading the cache, deciding
      whether the throttle window has expired, and reserving the slot
      — runs under the lock.
    * The Radarr/Sonarr HTTP call runs entirely outside the lock so a
      slow upstream cannot starve other workers' throttle reads.
    * Each attempt registers a unique reservation token; rollback on
      failure compares against that token instead of float-equality on
      the timestamp, so a sibling worker overwriting the slot can no
      longer silently no-op the rollback (Domain-06 #8).
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
    # check across all workers (Domain-06 #7).
    persisted_epoch, persisted_count = _load_throttle_from_db(conn, dl_id)

    # Phase 1: reserve the slot under the lock. Treat *now* as the
    # speculative trigger timestamp so concurrent siblings see the slot
    # taken; we'll roll this back if the network call ultimately fails.
    # ``my_token`` uniquely identifies this attempt so rollback can
    # detect a sibling thread having overwritten the reservation in the
    # meantime (Domain-06 #8).
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
    success = False
    try:
        client = build_arr_client(conn, service, secret_key)
        if client is None:
            return
        # ``service`` is paired with ``kind`` above (radarr↔movie, sonarr↔series),
        # so ``build_arr_client`` returns the matching subtype — narrow with cast
        # since mypy can't track the relationship.
        from mediaman.services.arr.radarr import RadarrClient
        from mediaman.services.arr.sonarr import SonarrClient

        if kind == "movie":
            cast(RadarrClient, client).search_movie(arr_id)
        else:
            cast(SonarrClient, client).search_series(arr_id)
        success = True
        # Late import: ``_deep_links`` lives in the ``download_queue`` package
        # whose ``__init__`` imports ``maybe_trigger_search`` from this module,
        # so a top-level import here would cycle. The post-fire log mirrors
        # the count update done inside the finally block (line below); keeping
        # the value inline avoids shadowing the ``new_count: int | None``
        # declaration the lock-protected commit path relies on.
        from mediaman.services.downloads.download_queue._deep_links import (
            _format_next_attempt,
        )

        logger.info(
            "Triggered search for stalled item %s (n=%d, %s)",
            dl_id,
            previous_count + 1,
            _format_next_attempt(_search_backoff_seconds(previous_count + 1, dl_id, now)),
        )
    except Exception:
        logger.warning("Failed to trigger search for %s", dl_id, exc_info=True)
    finally:
        # Phase 3: re-acquire the lock to commit or roll back. The token
        # check (Domain-06 #8) guards against a sibling thread that
        # overwrote our reservation between phases 1 and 3 — without it,
        # the prior float-equality on ``now`` could either silently
        # no-op our rollback (if the sibling stamped a newer ``now``)
        # or drop the sibling's reservation. With the token, we either
        # restore our prior value or quietly defer to whoever beat us
        # to the slot.
        new_count: int | None
        with _state_lock:
            if success:
                new_count = previous_count + 1
                _search_count[dl_id] = new_count
                # Mirror the timestamp under the arr-id-stable key so a
                # subsequent rename of the same series can't bypass the
                # throttle by producing a fresh title-derived dl_id
                # (Domain-06 #11).
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
        if success and new_count is not None:
            _save_trigger_to_db(conn, dl_id, now, new_count)


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

    now = time.time()
    for item in arr_items:
        item_dict = cast(dict, item)
        maybe_trigger_search(conn, item_dict, matched_nzb=False, secret_key=secret_key)
        try:
            maybe_auto_abandon(conn, secret_key, item=item_dict, now=now)
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
    arr_items: list[ArrCard],
    secret_key: str,
) -> None:
    """Fire SeriesSearch for Sonarr series with partial missing episodes.

    Dedupes against series already handled by the main pass via
    ``arr_id``, and reuses the ``sonarr:{title}`` dl_id format so the
    per-item throttle recognises the same series across passes.
    """
    from mediaman.services.arr.sonarr import SonarrClient

    client = build_arr_client(conn, "sonarr", secret_key)
    if client is None:
        return
    # ``service="sonarr"`` always yields a SonarrClient; narrow for mypy.
    sonarr_client = cast(SonarrClient, client)

    already_poked = {
        item.get("arr_id")
        for item in arr_items
        if item.get("kind") == "series" and item.get("arr_id")
    }

    # Domain-06 #11: the per-``dl_id`` throttle in ``maybe_trigger_search``
    # collapses under a Sonarr title rename — a renamed series produces a
    # fresh ``sonarr:{title}`` key on every tick, bypassing the throttle.
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
