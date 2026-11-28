"""SQLite persistence and inspection API for the Arr search throttle.

Owns the ``arr_search_throttle`` table interactions: loading, saving,
reconciling stranded rows, clearing per-item state, and resetting all
in-memory state.  All mutable in-memory state is imported from
``mediaman.services.arr._throttle_state`` so the two modules share
the same dicts and lock object.

Most of these names are re-exported from
``mediaman.services.arr.search_trigger``, which most callers import them
from.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Sequence, Set
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mediaman.core.time import now_utc
from mediaman.services.arr._throttle_state import (
    _arr_throttle_key,
    _last_search_trigger,
    _last_search_trigger_by_arr,
    _reservation_tokens,
    _search_count,
    _state_lock,
)

if TYPE_CHECKING:
    from mediaman.services.arr.fetcher._base import ArrCard

logger = logging.getLogger(__name__)

_STRANDED_THROTTLE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# Rate-limit for the persist-failure WARNING: a wedged disk would
# otherwise emit one WARNING per dl_id per tick, drowning the log. We log at
# most once per this window and fold the suppressed count into the next line.
# Best-effort in-process counters; reset on restart.
_PERSIST_WARN_INTERVAL_SECONDS = 60.0
_persist_warn_last: float = 0.0
_persist_warn_suppressed: int = 0


def _load_throttle_from_db(conn: sqlite3.Connection, dl_id: str) -> tuple[float, int] | None:
    """Return ``(last_triggered_epoch, search_count)`` for *dl_id*, or None where it can't be read.

    Reads from the ``arr_search_throttle`` table.  Returns ``(0.0, 0)``
    for an item mediaman has never poked.

    Exception policy: only ``sqlite3.DatabaseError`` (a locked or
    missing table) is swallowed, as None: the caller must not take an
    unread row for "never triggered", or a busy database just after a
    restart would fire every stalled search again and restart its count.
    Any other exception (e.g. a coding bug in the parser) propagates so
    the caller sees the real failure.
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
    # sqlite3.OperationalError is a subclass of sqlite3.DatabaseError;
    # listing the parent alone covers both. sqlite3.InterfaceError is
    # NOT caught here because it indicates programmer error (e.g. wrong
    # binding type), which should propagate, not be swallowed.
    except sqlite3.DatabaseError as exc:
        logger.warning("arr_search_trigger: could not read the throttle of %s: %s", dl_id, exc)
        return None


def _save_trigger_to_db(conn: sqlite3.Connection, dl_id: str, epoch: float, count: int) -> None:
    """Persist *epoch* and *count* for *dl_id*.

    Uses ``INSERT OR REPLACE`` so the upsert is idempotent.  Failures are
    logged and swallowed — the in-memory state is still correct even when
    the write fails (e.g. DB locked briefly).
    """
    try:
        ts = datetime.fromtimestamp(epoch, tz=UTC).isoformat()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO arr_search_throttle "
                "(key, last_triggered_at, search_count) VALUES (?, ?, ?)",
                (dl_id, ts, count),
            )
    # See _load_throttle_from_db: DatabaseError covers OperationalError.
    # sqlite3.InterfaceError is deliberately not caught (programmer error).
    except sqlite3.DatabaseError:
        # In-memory state stays correct for the running process; only the
        # persisted row is now behind. A missing/stale row is read back as
        # "unknown" (zeros) by _load_throttle_from_db / get_search_info, never
        # as "never fired", so the throttle re-warms rather than re-firing
        # blindly. Rate-limit the WARNING so a wedged disk can't spam one line
        # per dl_id per tick.
        _warn_persist_failure(dl_id)


def _warn_persist_failure(dl_id: str) -> None:
    """Emit a rate-limited WARNING for a throttle-persist failure.

    Logs at most once per ``_PERSIST_WARN_INTERVAL_SECONDS``, folding the
    number of suppressed failures since the last line into the message so the
    operator still sees the scale of a wedged disk without log flooding.
    """
    global _persist_warn_last, _persist_warn_suppressed
    now = time.monotonic()
    if now - _persist_warn_last < _PERSIST_WARN_INTERVAL_SECONDS:
        _persist_warn_suppressed += 1
        return
    suppressed = _persist_warn_suppressed
    _persist_warn_suppressed = 0
    _persist_warn_last = now
    logger.warning(
        "arr_search_trigger: failed to persist throttle for %s "
        "(%d further failures suppressed since last warning)",
        dl_id,
        suppressed,
        exc_info=True,
    )


def adopt_throttle(conn: sqlite3.Connection, cards: Sequence[ArrCard]) -> None:
    """Start each film from what was once counted under its title alone, unless its search
    began after that. The title's row is left for an earlier version to go on from.
    """
    # Late import: the fetcher package imports this module.
    from mediaman.services.arr.fetcher._base import is_film_id

    for card in cards:
        since = card.get("searching_since", 0.0)
        if not since or not is_film_id(card["dl_id"], card["title"]):
            continue
        searching_since = datetime.fromtimestamp(since, tz=UTC).isoformat()
        try:
            row = conn.execute(
                "SELECT last_triggered_at, search_count FROM arr_search_throttle"
                " WHERE key=? AND last_triggered_at >= ?"
                " AND NOT EXISTS (SELECT 1 FROM arr_search_throttle WHERE key=?)",
                (f"radarr:{card['title']}", searching_since, card["dl_id"]),
            ).fetchone()
            if row is not None:
                with conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO arr_search_throttle"
                        " (key, last_triggered_at, search_count) VALUES (?, ?, ?)",
                        (card["dl_id"], row[0], row[1]),
                    )
        except sqlite3.DatabaseError:
            logger.warning(
                "arr_search_trigger: failed to carry the searches of %s over",
                card["dl_id"],
                exc_info=True,
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

        loaded = _load_throttle_from_db(get_db(), dl_id)
    # DatabaseError covers OperationalError; ``RuntimeError`` covers
    # ``get_db()`` raising because the FastAPI request-local connection
    # has gone away (e.g. background-thread call outside a request scope).
    # sqlite3.InterfaceError is deliberately not caught (programmer error).
    except (sqlite3.DatabaseError, RuntimeError):
        logger.warning(
            "arr_search_trigger.get_search_info: DB fallback failed for "
            "dl_id=%s — reporting zero pair",
            dl_id,
            exc_info=True,
        )
        return 0, 0.0
    if loaded is None or loaded == (0.0, 0):
        return 0, 0.0
    epoch, persisted_count = loaded

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
    in ``ttl_seconds`` (default 30 days), the item is either deleted
    or so deeply forgotten that resetting its search-count is the
    desired behaviour.  An item still searched for is searched at least
    once a day (the backoff's cap), so its row is never reaped.  The
    in-memory counts of what is reaped go too, or the next search would
    carry the old count back.

    A stalled DB returns 0 rather than raising so a slow disk doesn't
    break boot.

    Args:
        conn: Open SQLite connection.
        ttl_seconds: Cutoff age in seconds. Rows whose
            ``last_triggered_at`` is older than ``now - ttl_seconds``
            are deleted.

    Returns:
        Number of rows deleted.
    """
    cutoff_dt = now_utc() - timedelta(seconds=ttl_seconds)
    cutoff = cutoff_dt.isoformat()
    with _state_lock:
        for dl_id, last in list(_last_search_trigger.items()):
            if last < cutoff_dt.timestamp():
                _last_search_trigger.pop(dl_id, None)
                _search_count.pop(dl_id, None)
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM arr_search_throttle WHERE last_triggered_at < ?",
                (cutoff,),
            )
    # DatabaseError covers OperationalError. InterfaceError deliberately
    # excluded (programmer error, not a transient DB hiccup).
    except sqlite3.DatabaseError:
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


def forget_unwanted_throttle(conn: sqlite3.Connection, service: str, wanted: Set[str]) -> None:
    """Forget the searches of each title of *service* that is not in *wanted*.

    *wanted* is every dl_id *service* still searches for, from a whole answer: a title
    that imported, was unmonitored or was deleted counts its searches from none if it is
    wanted again. The title row an earlier version keeps for a film still wanted stays.
    """

    def unwanted(dl_id: str) -> bool:
        return dl_id not in wanted and not any(w.startswith(f"{dl_id}#") for w in wanted)

    prefix = f"{service}:"
    with _state_lock:
        for dl_id in _search_count.keys() | _last_search_trigger.keys():
            if dl_id.startswith(prefix) and unwanted(dl_id):
                _search_count.pop(dl_id, None)
                _last_search_trigger.pop(dl_id, None)
    try:
        rows = conn.execute(
            "SELECT key FROM arr_search_throttle WHERE substr(key, 1, ?) = ?",
            (len(prefix), prefix),
        ).fetchall()
        gone = [(row[0],) for row in rows if unwanted(row[0])]
        if gone:
            with conn:
                conn.executemany("DELETE FROM arr_search_throttle WHERE key=?", gone)
    except sqlite3.DatabaseError:
        logger.warning(
            "arr_search_trigger: failed to forget what %s no longer searches for",
            service,
            exc_info=True,
        )


def clear_throttle(conn: sqlite3.Connection, dl_id: str, *, service: str | None = None) -> None:
    """Forget every trace of *dl_id* from the search-throttle subsystem.

    Removes the persisted ``arr_search_throttle`` row and drops the entry
    from the in-memory caches.  Used by the abandon flow so a future
    re-monitor starts a fresh search-count rather than inheriting a stale
    "Searched 52×" hint.

    *service* ("radarr"/"sonarr"), when supplied, also pops the per-arr-
    instance fan-out-cap entry so an abandon immediately followed by a
    re-monitor of the same item can search straight away instead of waiting
    out the remaining _PER_ARR_THROTTLE_SECONDS window. The abandon flow
    always knows the service, so it always passes it; the parameter is
    optional only to keep older call sites compiling.

    Idempotent: calling on a key that was never seen is a no-op.
    """
    with _state_lock:
        _last_search_trigger.pop(dl_id, None)
        _search_count.pop(dl_id, None)
        if service is not None:
            _last_search_trigger_by_arr.pop(_arr_throttle_key(service), None)
    try:
        with conn:
            conn.execute("DELETE FROM arr_search_throttle WHERE key=?", (dl_id,))
    # DatabaseError covers OperationalError. InterfaceError deliberately
    # excluded (programmer error).
    except sqlite3.DatabaseError:
        logger.warning("arr_search_trigger: failed to clear throttle for %s", dl_id, exc_info=True)


def reset_search_triggers() -> None:
    """Clear the in-memory search-trigger snapshot. Used by tests."""
    _last_search_trigger.clear()
    _search_count.clear()
    _reservation_tokens.clear()
    _last_search_trigger_by_arr.clear()
