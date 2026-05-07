"""SQLite persistence and inspection API for the Arr search throttle.

Owns the ``arr_search_throttle`` table interactions: loading, saving,
reconciling stranded rows, clearing per-item state, and resetting all
in-memory state.  All mutable in-memory state is imported from
:mod:`mediaman.services.arr._throttle_state` so the two modules share
the same dicts and lock object.

All names in this module are re-exported verbatim from
:mod:`mediaman.services.arr.search_trigger` so existing import paths
and test monkeypatch targets continue to work.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC

from mediaman.services.arr._throttle_state import (
    _last_search_trigger,
    _last_search_trigger_by_arr,
    _reservation_tokens,
    _search_count,
    _state_lock,
)

logger = logging.getLogger(__name__)

_STRANDED_THROTTLE_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days


def _load_throttle_from_db(conn: sqlite3.Connection, dl_id: str) -> tuple[float, int]:
    """Return ``(last_triggered_epoch, search_count)`` for *dl_id*.

    Reads from the ``arr_search_throttle`` table.  Returns ``(0.0, 0)``
    when the table or row doesn't exist yet (pre-migration DBs during
    startup, or items mediaman has never poked).

    Exception policy: only ``sqlite3.OperationalError`` and
    ``sqlite3.DatabaseError`` are swallowed — those genuinely represent
    transient or pre-migration states where ``(0.0, 0)`` is the correct
    fallback.  A broader ``except Exception`` previously masked
    schema/migration faults too, silently disabling the throttle by
    reporting "never triggered" for every dl_id.  Any other exception
    (e.g. a coding bug in the parser) now propagates so the caller sees
    the real failure.
    """
    try:
        row = conn.execute(
            "SELECT last_triggered_at, search_count FROM arr_search_throttle WHERE key=?",
            (dl_id,),
        ).fetchone()
        if row is None:
            return 0.0, 0
        from mediaman.core.time import parse_iso_utc

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

    Connection caveat: the DB-fallback branch calls
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
    # missing table on a fresh install, ``get_db()`` raising because the
    # request context has gone away) returns the zero pair rather than
    # raising, so a stalled connection never breaks the page. Logged at
    # ``warning`` with ``exc_info`` so a silent class of failure can't
    # masquerade as "no search has fired yet".
    try:
        from mediaman.db import get_db

        epoch, persisted_count = _load_throttle_from_db(get_db(), dl_id)
    except Exception:
        logger.warning(
            "arr_search_trigger.get_search_info: DB fallback failed for "
            "dl_id=%s — reporting zero pair",
            dl_id,
            exc_info=True,
        )
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

    Rows in ``arr_search_throttle`` accumulate forever when an item is
    deleted from Radarr/Sonarr — nothing else references the row, but
    ``clear_throttle`` is only called by the abandon flow. Operators who
    delete items directly via the Radarr/Sonarr UI never trip that path,
    so the table grows monotonically.

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
