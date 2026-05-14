"""Protection and snooze queries on ``scheduled_actions`` and ``kept_shows``.

Split from :mod:`scheduled_actions` so the protection/snooze read path lives
in its own module while deletion-lifecycle mutations stay in
:mod:`scheduled_actions`. Both modules write to the same table group; this
module is read-heavy (or small targeted DELETEs) and never touches
``DeletionRow``.
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.core.time import now_iso

logger = logging.getLogger(__name__)


def is_protected(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if the item has an active protection action.

    An item is protected if **any** of its ``scheduled_actions`` rows
    has ``action='protected_forever'`` (regardless of ``token_used``),
    or if **any** of its ``snoozed`` rows still has ``execute_at`` in
    the future.

    A ``protected_forever`` row is authoritative regardless of where it
    sits in id order: the schema does not enforce one-row-per-item and
    row order is not a contract, so an earlier ``protected_forever`` row
    must not be masked by a later expired ``snoozed`` row. The single
    ``OR`` query below checks both protective states in one round trip —
    a match on either branch means the item is kept.
    """
    return (
        conn.execute(
            "SELECT 1 FROM scheduled_actions "
            "WHERE media_item_id = ? "
            "  AND ("
            "    action = 'protected_forever'"
            "    OR (action = 'snoozed' AND execute_at IS NOT NULL AND execute_at > ?)"
            "  ) "
            "LIMIT 1",
            (media_id, now_iso()),
        ).fetchone()
        is not None
    )


def is_already_scheduled(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if deletion is already pending for this item."""
    row = conn.execute(
        """
        SELECT id FROM scheduled_actions
        WHERE media_item_id = ? AND action = 'scheduled_deletion' AND token_used = 0
        LIMIT 1
        """,
        (media_id,),
    ).fetchone()
    return row is not None


def has_expired_snooze(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if the item has a prior snoozed action that was consumed."""
    row = conn.execute(
        """
        SELECT id FROM scheduled_actions
        WHERE media_item_id = ? AND action = 'snoozed' AND token_used = 1
        LIMIT 1
        """,
        (media_id,),
    ).fetchone()
    return row is not None


def is_show_kept_pure(
    conn: sqlite3.Connection,
    show_rating_key: str | None,
    *,
    now_iso_str: str | None = None,
) -> bool:
    """Pure read for ``is_show_kept``: returns True iff a live keep rule exists.

    Performs **no** writes. Used directly by callers that want a clean
    boolean answer without touching the DB; :func:`is_show_kept` wraps
    this with the legacy expired-snooze cleanup to preserve the
    pre-existing engine.py contract.
    """
    if not show_rating_key:
        return False
    now = now_iso_str or now_iso()
    row = conn.execute(
        """
        SELECT action, execute_at FROM kept_shows
        WHERE show_rating_key = ?
        LIMIT 1
        """,
        (show_rating_key,),
    ).fetchone()
    if row is None:
        return False
    if row["action"] == "protected_forever":
        return True
    return bool(row["execute_at"] and row["execute_at"] > now)


def cleanup_expired_show_snoozes(conn: sqlite3.Connection, now_iso: str) -> int:
    """Delete every ``kept_shows`` row whose snoozed keep has lapsed.

    ``protected_forever`` rows have ``execute_at IS NULL`` and are
    therefore left untouched. Returns the row count removed so callers
    can log / metric the cleanup.

    Pulled out of :func:`is_show_kept` so the cleanup is a separately
    callable, observable, single-purpose operation. Suitable for a
    periodic scheduler job — a single statement covers every expired
    row in one round trip — but :func:`is_show_kept` still calls it
    inline for back-compat with the engine.py caller that expects the
    legacy "ask + clean" behaviour.
    """
    cur = conn.execute(
        "DELETE FROM kept_shows "
        "WHERE action = 'snoozed' "
        "AND execute_at IS NOT NULL "
        "AND execute_at <= ?",
        (now_iso,),
    )
    return cur.rowcount or 0


def is_show_kept(conn: sqlite3.Connection, show_rating_key: str | None) -> bool:
    """Return True if the show has an active keep rule in ``kept_shows``.

    Composed from two single-purpose helpers so each side is observable
    in isolation:

    * :func:`is_show_kept_pure` — the read.
    * :func:`cleanup_expired_show_snoozes` — the cleanup.

    The split makes each side observable in isolation. This top-level
    function preserves the legacy "ask + clean" contract for the
    existing engine.py caller: when the read says the keep is no
    longer live (i.e. an expired snooze), we sweep the row out so the
    table doesn't accrete dead rows over time. Callers that want a
    pure read should call :func:`is_show_kept_pure` directly.
    """
    if not show_rating_key:
        return False
    now = now_iso()
    kept = is_show_kept_pure(conn, show_rating_key, now_iso_str=now)
    if not kept:
        # Either the row is missing or its snooze has lapsed; ask the
        # cleanup helper to remove any expired row for this key. Doing
        # the cleanup at most once per call (and only on the no-longer-
        # kept path) keeps the read fast for the common protected_forever
        # case.
        cleanup_expired_show_snoozes(conn, now)
    return kept


def fetch_protected_media_ids(
    conn: sqlite3.Connection,
    media_ids: list[str],
    now_iso_str: str,
) -> set[str]:
    """Return the subset of *media_ids* that are protected.

    Batched, set-building replacement for the per-item
    :func:`is_protected` round trip in the hot scan loop (§13.3). The
    active-ness rule matches :func:`is_protected` byte-for-byte: a
    ``protected_forever`` row (any ``token_used``) or a ``snoozed`` row
    whose ``execute_at`` is non-NULL and in the future. Chunked into
    groups of 500 to stay below SQLite's parameter limit.
    """
    if not media_ids:
        return set()
    found: set[str] = set()
    for start in range(0, len(media_ids), 500):
        chunk = media_ids[start : start + 500]
        id_placeholders = ",".join("?" * len(chunk))
        # rationale: §9.6 IN-clause batching — only "?" placeholders interpolated; every value is bound
        rows = conn.execute(  # nosec B608
            f"SELECT DISTINCT media_item_id FROM scheduled_actions "
            f"WHERE media_item_id IN ({id_placeholders}) "
            f"  AND ("
            f"    action = 'protected_forever'"
            f"    OR (action = 'snoozed' AND execute_at IS NOT NULL AND execute_at > ?)"
            f"  )",
            (*chunk, now_iso_str),
        ).fetchall()
        found.update(r["media_item_id"] for r in rows)
    return found


def fetch_already_scheduled_media_ids(
    conn: sqlite3.Connection,
    media_ids: list[str],
) -> set[str]:
    """Return the subset of *media_ids* with a pending ``scheduled_deletion``.

    Batched, set-building replacement for the per-item
    :func:`is_already_scheduled` round trip in the hot scan loop
    (§13.3). The rule matches :func:`is_already_scheduled` exactly:
    ``action = 'scheduled_deletion'`` with ``token_used = 0``. Chunked
    into groups of 500 to stay below SQLite's parameter limit.
    """
    if not media_ids:
        return set()
    found: set[str] = set()
    for start in range(0, len(media_ids), 500):
        chunk = media_ids[start : start + 500]
        id_placeholders = ",".join("?" * len(chunk))
        # rationale: §9.6 IN-clause batching — only "?" placeholders interpolated; every value is bound
        rows = conn.execute(  # nosec B608
            f"SELECT DISTINCT media_item_id FROM scheduled_actions "
            f"WHERE media_item_id IN ({id_placeholders}) "
            f"  AND action = 'scheduled_deletion' AND token_used = 0",
            tuple(chunk),
        ).fetchall()
        found.update(r["media_item_id"] for r in rows)
    return found


def cleanup_expired_snoozes(conn: sqlite3.Connection, now_iso: str) -> None:
    """Remove expired ``snoozed`` rows so items re-enter the scan pipeline."""
    conn.execute(
        "DELETE FROM scheduled_actions WHERE action = 'snoozed' AND execute_at < ?",
        (now_iso,),
    )
