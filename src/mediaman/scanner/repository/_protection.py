"""Protection and snooze queries on ``scheduled_actions`` and ``kept_shows``.

The protection/snooze read path lives here; deletion-lifecycle
mutations live in scheduled_actions. Both modules write to the same table group; this
module is read-heavy (or small targeted DELETEs) and never touches
``DeletionRow``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection

from mediaman.core.audit import log_audit
from mediaman.core.time import now_iso

logger = logging.getLogger(__name__)


def is_protected(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if the item has an active protection action.

    An item is protected if **any** of its ``scheduled_actions`` rows
    has ``action='protected_forever'`` (regardless of ``token_used``),
    or if **any** of its ``snoozed`` rows still has ``execute_at`` at or
    after *now*. The boundary is inclusive (``>=``) so the protection
    side owns the exact-``now`` instant; cleanup_expired_snoozes
    uses the complementary strict ``<`` so the two never overlap and a
    snooze whose ``execute_at`` equals *now* is treated as still active.

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
            "    OR (action = 'snoozed' AND execute_at IS NOT NULL AND execute_at >= ?)"
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
    boolean answer without touching the DB; is_show_kept wraps
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

    Pulled out of is_show_kept so the cleanup is a separately
    callable, observable, single-purpose operation. Suitable for a
    periodic scheduler job — a single statement covers every expired
    row in one round trip — but is_show_kept still calls it inline so
    that asking also cleans up.
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

    * is_show_kept_pure — the read.
    * cleanup_expired_show_snoozes — the cleanup.

    The split makes each side observable in isolation. This top-level
    function preserves the legacy "ask + clean" contract for the
    existing engine.py caller: when the read says the keep is no
    longer live (i.e. an expired snooze), we sweep the row out so the
    table doesn't accrete dead rows over time. Callers that want a
    pure read should call is_show_kept_pure directly.
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
    is_protected round trip in the hot scan loop. The
    active-ness rule matches is_protected byte-for-byte: a
    ``protected_forever`` row (any ``token_used``) or a ``snoozed`` row
    whose ``execute_at`` is non-NULL and at or after *now* (inclusive
    ``>=`` boundary, matching is_protected). Chunked into
    groups of 500 to stay below SQLite's parameter limit.
    """
    if not media_ids:
        return set()
    found: set[str] = set()
    for start in range(0, len(media_ids), 500):
        chunk = media_ids[start : start + 500]
        id_placeholders = ",".join("?" * len(chunk))
        # rationale: IN-clause batching — only "?" placeholders interpolated; every value is bound
        rows = conn.execute(  # nosec B608
            f"SELECT DISTINCT media_item_id FROM scheduled_actions "
            f"WHERE media_item_id IN ({id_placeholders}) "
            f"  AND ("
            f"    action = 'protected_forever'"
            f"    OR (action = 'snoozed' AND execute_at IS NOT NULL AND execute_at >= ?)"
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
    is_already_scheduled round trip in the hot scan loop. The rule matches
    is_already_scheduled exactly: ``action = 'scheduled_deletion'`` with
    ``token_used = 0``. Chunked into groups of 500 to stay below SQLite's
    parameter limit.
    """
    if not media_ids:
        return set()
    found: set[str] = set()
    for start in range(0, len(media_ids), 500):
        chunk = media_ids[start : start + 500]
        id_placeholders = ",".join("?" * len(chunk))
        # rationale: IN-clause batching — only "?" placeholders interpolated; every value is bound
        rows = conn.execute(  # nosec B608
            f"SELECT DISTINCT media_item_id FROM scheduled_actions "
            f"WHERE media_item_id IN ({id_placeholders}) "
            f"  AND action = 'scheduled_deletion' AND token_used = 0",
            tuple(chunk),
        ).fetchall()
        found.update(r["media_item_id"] for r in rows)
    return found


def fetch_kept_show_keys(
    conn: sqlite3.Connection,
    show_rating_keys: list[str],
    now_iso_str: str,
) -> set[str]:
    """Return the subset of *show_rating_keys* with a live keep rule.

    Batched, set-building replacement for the per-season
    is_show_kept_pure round trip in the TV scan loop. The
    live-keep rule matches is_show_kept_pure exactly: a
    ``protected_forever`` row, or a row whose ``execute_at`` is non-NULL
    and still in the future. ``kept_shows.show_rating_key`` is ``UNIQUE``
    (see the schema), so there is at most one row per key and the set
    answer equals the helper's ``LIMIT 1`` read byte-for-byte. Chunked
    into groups of 500 to stay below SQLite's parameter limit.

    Unlike is_show_kept, this performs **no** cleanup write: the
    caller sweeps expired snoozes once up front via
    cleanup_expired_show_snoozes before building this set.
    """
    if not show_rating_keys:
        return set()
    found: set[str] = set()
    for start in range(0, len(show_rating_keys), 500):
        chunk = show_rating_keys[start : start + 500]
        key_placeholders = ",".join("?" * len(chunk))
        # rationale: IN-clause batching — only "?" placeholders interpolated; every value is bound
        rows = conn.execute(  # nosec B608
            f"SELECT show_rating_key FROM kept_shows "
            f"WHERE show_rating_key IN ({key_placeholders}) "
            f"  AND ("
            f"    action = 'protected_forever'"
            f"    OR (execute_at IS NOT NULL AND execute_at > ?)"
            f"  )",
            (*chunk, now_iso_str),
        ).fetchall()
        found.update(r["show_rating_key"] for r in rows)
    return found


def cleanup_expired_snoozes(conn: sqlite3.Connection, now_iso: str) -> None:
    """Remove expired *unconsumed* ``snoozed`` rows so items re-enter scanning.

    Scoped to ``token_used = 0`` deliberately. A ``snoozed`` row with
    ``token_used = 1`` is the audit-grade record that the user once asked
    to keep this item, and it is the **sole** re-entry signal read by
    has_expired_snooze (which matches only consumed rows). Sweeping
    consumed rows here would destroy that signal, so an item whose snooze
    expired more than one deletion-pass ago would be re-scheduled with
    ``is_reentry = False`` and a normal grace period instead of being
    flagged as a re-entry. Unconsumed expired snoozes carry no re-entry
    meaning and are still swept so they cannot mask a fresh evaluation.
    """
    conn.execute(
        "DELETE FROM scheduled_actions "
        "WHERE action = 'snoozed' AND token_used = 0 AND execute_at < ?",
        (now_iso,),
    )


_KEEP_COLUMNS = "action, scheduled_at, execute_at, token_used, snoozed_at, snooze_duration"


def save_pruned_keeps(conn: sqlite3.Connection, media_ids: Collection[str]) -> None:
    """Set aside the live keeps of items about to be removed, and forget lapsed ones.

    Run it in the transaction that removes the items, and carry_pruned_keeps after it.
    """
    now = now_iso()
    conn.execute("DELETE FROM pruned_keeps WHERE action = 'snoozed' AND execute_at < ?", (now,))
    # rationale: the interpolated column list is a fixed string; every value is bound
    conn.executemany(  # nosec B608
        "INSERT INTO pruned_keeps (media_item_id, file_path, season_number, tvdb_id, tmdb_id, "
        f"plex_library_id, {_KEEP_COLUMNS}) "
        "SELECT mi.id, mi.file_path, mi.season_number, "
        # The show's ids do not tell a season that has no number.
        "CASE WHEN (mi.media_type = 'movie') = (mi.season_number IS NULL) THEN mi.tvdb_id END, "
        "CASE WHEN (mi.media_type = 'movie') = (mi.season_number IS NULL) "
        "  THEN COALESCE(mi.tmdb_id, mi.last_tmdb_id) END, "
        f"mi.plex_library_id, {_KEEP_COLUMNS} "
        "FROM scheduled_actions sa JOIN media_items mi ON mi.id = sa.media_item_id "
        "WHERE mi.id = ? AND (sa.action = 'protected_forever' "
        "  OR (sa.action = 'snoozed' AND sa.execute_at >= ?))",
        [(media_id, now) for media_id in media_ids],
    )


def take_pruned_keeps(
    conn: sqlite3.Connection, media_id: str, file_path: str, season_number: object
) -> list[sqlite3.Row]:
    """Remove and return the live keeps set aside for this item.

    Plex gives an item it removed and found again a new key, so one that is
    not in the library yet also takes those of its path and season.
    """
    keeps = conn.execute(
        "SELECT * FROM pruned_keeps "
        "WHERE (action = 'protected_forever' OR execute_at >= ?) "
        "  AND (media_item_id = ? OR (file_path != '' AND file_path = ? AND season_number IS ? "
        "    AND NOT EXISTS (SELECT 1 FROM media_items WHERE id = ?)))",
        (now_iso(), media_id, file_path, season_number, media_id),
    ).fetchall()
    conn.executemany("DELETE FROM pruned_keeps WHERE id = ?", [(keep["id"],) for keep in keeps])
    return keeps


def fetch_replaced_keep(
    conn: sqlite3.Connection,
    media_id: str,
    file_path: str,
    season_number: object,
    listed: Collection[str],
) -> list[sqlite3.Row]:
    """Return the longest live keep of the item *media_id* replaces, none if it replaces no item.

    Plex gives an item a new key and drops the old one, which is still in the
    library: of the same path and season, and not in *listed*, the keys Plex
    lists now. Only a key not in the library yet replaces another. The keep is
    left on the old key too, as a list that is too short may miss it.
    """
    rows = conn.execute(
        "SELECT sa.* FROM media_items mi JOIN scheduled_actions sa ON sa.media_item_id = mi.id "
        "WHERE mi.file_path = ? AND mi.file_path != '' AND mi.season_number IS ? "
        "  AND (sa.action = 'protected_forever' OR (sa.action = 'snoozed' AND sa.execute_at >= ?)) "
        "  AND NOT EXISTS (SELECT 1 FROM media_items WHERE id = ?) "
        "ORDER BY sa.action = 'protected_forever' DESC, sa.execute_at DESC",
        (file_path, season_number, now_iso(), media_id),
    )
    return [row for row in rows if row["media_item_id"] not in listed][:1]


def restore_keeps(conn: sqlite3.Connection, media_id: str, keeps: list[sqlite3.Row]) -> None:
    """Put *keeps*, of take_pruned_keeps, fetch_replaced_keep or pruned_keeps, on the item.

    Each is audited.
    """
    # rationale: the interpolated column list is a fixed string; every value is bound
    conn.executemany(  # nosec B608
        f"INSERT INTO scheduled_actions (media_item_id, {_KEEP_COLUMNS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(media_id, *(keep[c] for c in _KEEP_COLUMNS.split(", "))) for keep in keeps],
    )
    for keep in keeps:
        log_audit(
            conn,
            media_id,
            "kept" if keep["action"] == "protected_forever" else "snoozed",
            "Keep given back: Plex lists the title again"
            + ("" if keep["media_item_id"] == media_id else " under a new key"),
        )
