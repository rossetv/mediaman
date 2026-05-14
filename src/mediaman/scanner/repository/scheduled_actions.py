"""SQL operations on the `scheduled_actions`, `kept_shows`, and `snoozes` tables."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from mediaman.core.time import now_iso

logger = logging.getLogger(__name__)

# The action that means deletion is already lined up.
DELETION_ACTION = "scheduled_deletion"

# Default token TTL: 30 days from now.
_TOKEN_TTL_DAYS = 30


@dataclass(frozen=True, slots=True)
class DeletionRow:
    """A pending or stuck deletion joined with its ``media_items`` row.

    Returned by :func:`fetch_pending_deletions` and
    :func:`fetch_stuck_deletions` so the deletion executor consumes typed
    attributes instead of a raw :class:`sqlite3.Row`. Both queries select
    the same column set; columns a given query does not logically need
    (``action`` for the pending path, the *arr ids for the stuck path)
    are still selected so the dataclass shape is uniform — the executor
    simply does not read them on that path.
    """

    id: int
    media_item_id: str
    action: str | None
    file_path: str | None
    file_size_bytes: int | None
    title: str | None
    plex_rating_key: str | None
    radarr_id: int | None
    sonarr_id: int | None
    season_number: int | None


def _row_to_deletion_row(row: sqlite3.Row) -> DeletionRow:
    """Map a joined ``scheduled_actions``/``media_items`` row to a :class:`DeletionRow`."""
    return DeletionRow(
        id=row["id"],
        media_item_id=row["media_item_id"],
        action=row["action"],
        file_path=row["file_path"],
        file_size_bytes=row["file_size_bytes"],
        title=row["title"],
        plex_rating_key=row["plex_rating_key"],
        radarr_id=row["radarr_id"],
        sonarr_id=row["sonarr_id"],
        season_number=row["season_number"],
    )


# ---------------------------------------------------------------------------
# scheduled_actions — protection / schedule queries
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# scheduled_actions — mutations
# ---------------------------------------------------------------------------


def fetch_stuck_deletions(conn: sqlite3.Connection) -> list[DeletionRow]:
    """Return rows in ``scheduled_actions`` still marked ``deleting``.

    Returns an empty list if the ``delete_status`` column has not been
    migrated yet (older DB schemas).
    """
    try:
        rows = conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.action, mi.file_path, "
            "mi.file_size_bytes, mi.title, mi.plex_rating_key, "
            "mi.radarr_id, mi.sonarr_id, mi.season_number "
            "FROM scheduled_actions sa "
            "LEFT JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.delete_status = 'deleting'"
        ).fetchall()
    except sqlite3.OperationalError:
        # delete_status column not yet migrated — nothing to do.
        return []
    return [_row_to_deletion_row(row) for row in rows]


def fetch_pending_deletions(conn: sqlite3.Connection, now_iso: str) -> list[DeletionRow]:
    """Return all pending deletions whose grace period has elapsed."""
    rows = conn.execute(
        "SELECT sa.id, sa.media_item_id, sa.action, mi.file_path, mi.file_size_bytes, "
        "mi.radarr_id, mi.sonarr_id, mi.season_number, mi.title, mi.plex_rating_key "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.action = 'scheduled_deletion' "
        "  AND sa.execute_at < ? "
        "  AND (sa.delete_status IS NULL OR sa.delete_status = 'pending')",
        (now_iso,),
    ).fetchall()
    return [_row_to_deletion_row(row) for row in rows]


def mark_delete_status(conn: sqlite3.Connection, action_id: int, status: str) -> None:
    """Set ``scheduled_actions.delete_status`` for the given row id."""
    conn.execute(
        "UPDATE scheduled_actions SET delete_status = ? WHERE id = ?",
        (status, action_id),
    )


def delete_scheduled_action(conn: sqlite3.Connection, action_id: int) -> None:
    """Remove a row from ``scheduled_actions``."""
    conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (action_id,))


def delete_actions_for_media_items(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Delete every ``scheduled_actions`` row pointing at *ids*, in chunks.

    Owned by this module because ``scheduled_actions`` is this
    repository's table-group. The scanner's delete phase calls this
    *before* :func:`media_items.delete_media_items` while holding the
    same transaction at the call site, so the two DELETEs stay atomic
    and a crash between them cannot orphan a ``scheduled_actions`` row
    against a deleted ``media_items`` row. This function therefore opens
    no transaction of its own.
    """
    if not ids:
        return
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        # rationale: §9.6 IN-clause batching — only "?" placeholders interpolated; every value is bound
        conn.execute(  # nosec B608
            f"DELETE FROM scheduled_actions WHERE media_item_id IN ({placeholders})",
            tuple(chunk),
        )


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


def count_pending_deletions(conn: sqlite3.Connection) -> int:
    """Return the number of pending ``scheduled_deletion`` rows.

    Counts only rows whose token has not yet been used — the same set
    :func:`clear_pending_deletions` sweeps. Snoozes and protect-forever
    rows are not counted.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions "
            "WHERE action='scheduled_deletion' AND token_used=0"
        ).fetchone()[0]
    )


def clear_pending_deletions(
    conn: sqlite3.Connection,
    *,
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> int:
    """Delete every pending ``scheduled_deletion`` row in one transaction.

    Returns the number of rows removed so callers can surface the count
    in their HTTP response. Snoozes and protect-forever rows are
    untouched — this only sweeps pending deletions whose token has not
    yet been used.

    Audit-in-transaction: when *audit_actor* is supplied, a
    ``sec:scan.cleared`` row is written inside the same
    ``BEGIN IMMEDIATE`` that deletes the rows. The pre-delete count
    lands in the audit detail. If the audit insert raises, the entire
    delete rolls back — we never end up with rows removed but no audit
    trail (the audit insert and the delete are atomic by design; neither
    commits without the other). The count read is delegated to
    :func:`count_pending_deletions` so the count is independently
    callable.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        cleared = count_pending_deletions(conn)
        conn.execute(
            "DELETE FROM scheduled_actions WHERE action='scheduled_deletion' AND token_used=0"
        )
        if audit_actor is not None:
            from mediaman.core.audit import security_event_or_raise

            security_event_or_raise(
                conn,
                event="scan.cleared",
                actor=audit_actor,
                ip=audit_ip,
                detail={"count": cleared},
            )
    return cleared
