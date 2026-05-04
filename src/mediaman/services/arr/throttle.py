"""In-memory and persisted throttle state for auto-triggered Arr searches.

This module owns:

* The module-level dicts (``_last_search_trigger``, ``_search_count``,
  ``_reservation_tokens``, ``_last_search_trigger_by_arr``) keyed by
  ``dl_id`` or by the stable ``service:#arr_id`` parallel key.
* The lock guarding every mutation of those dicts (``_state_lock``).
* Persistence to/from the ``arr_search_throttle`` SQLite table.
* The reconciliation pass that reaps stranded rows after a TTL.
* Inspection / reset helpers used by the UI and tests.

Split out of :mod:`mediaman.services.arr.search_trigger` so the
trigger-decision state machine has a focused home and the throttle
plumbing can be unit-tested independently. Public names here are
re-exported from :mod:`mediaman.services.arr.search_trigger` for
backwards compatibility with callers and tests that import from there.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC

from mediaman.services.infra.backoff import ExponentialBackoff

logger = logging.getLogger("mediaman")

# Module-level throttle for auto-triggered Radarr/Sonarr searches.
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


def _jitter_for(dl_id: str, last_triggered_at: float) -> float:
    """Return the deterministic ±10% jitter multiplier for *(dl_id, last_triggered_at)*.

    Kept as a thin shim so existing tests can ``monkeypatch`` it to
    pin the multiplier to a constant when asserting on the unjittered
    backoff curve.  Production code routes through ``_SEARCH_BACKOFF.delay``,
    which calls into :class:`~mediaman.services.infra.backoff.ExponentialBackoff`'s
    deterministic-multiplier helper using the same seed.
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

    Routes the multiplier through the module-level :func:`_jitter_for`
    shim so test monkeypatches on that name continue to override the
    multiplier as before.
    """
    n = max(search_count, 0)
    base = min(_SEARCH_BACKOFF_BASE_SECONDS * 2 ** max(n - 1, 0), _SEARCH_BACKOFF_MAX_SECONDS)
    return min(base * _jitter_for(dl_id, last_triggered_at), _SEARCH_BACKOFF_MAX_SECONDS)


def reset_search_triggers() -> None:
    """Clear the in-memory search-trigger snapshot. Used by tests."""
    _last_search_trigger.clear()
    _search_count.clear()
    _reservation_tokens.clear()
    _last_search_trigger_by_arr.clear()


def _arr_throttle_key(service: str, arr_id: int) -> str:
    """Return the stable arr-id-based throttle key.

    Used as a parallel index to ``_last_search_trigger`` (which is keyed
    by ``dl_id``). The ``dl_id`` collapses under a title rename;
    the arr-id key does not. ``maybe_trigger_search`` updates both, so
    any path that has access to ``(service, arr_id)`` can dedupe even
    if the title has changed since the last trigger (Domain-06 #11).
    """
    return f"{service}:#{arr_id}"


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
