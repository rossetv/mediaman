"""Delete phase — remove orphaned ``media_items`` rows after a scan.

An orphan is a ``media_items`` row whose ``plex_rating_key`` was not seen
during the most recent Plex fetch for the libraries that were successfully
scanned.  A suspiciously large drop (a Plex auth hiccup returning zero items
looks identical to a genuine mass-deletion on a single scan) is not acted on
immediately: the first such scan marks the library *pending* and skips, and
only a second consecutive suspicious scan confirms the drop and prunes. A
one-off glitch recovers before the second scan; a real deletion persists and
reconciles. See remove_orphans.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from mediaman.core.time import now_iso
from mediaman.scanner import repository

logger = logging.getLogger(__name__)

# Fail-closed safeguard thresholds for orphan detection.
# If the current scan found fewer items than this floor and the previous
# count met it, treat the scan as suspicious (prevents a zero-result scan
# from wiping the DB on a transient Plex hiccup).
_MIN_ITEMS_TO_TRUST = 5
# Only apply the ratio floor when the previous item count was at least
# this large (avoids false positives on small libraries).
_MIN_ITEMS_FOR_RATIO_CHECK = 50
# Minimum fraction of the previous item count that the current scan must
# return before orphan removal is trusted. A huge drop (e.g. 5 of 200)
# is suspicious.
_MIN_RATIO_TO_TRUST = 0.10

# Settings key holding the set of library ids whose previous scan tripped
# the suspicious-drop guard and are awaiting a second confirming scan.
_PENDING_GUARD_KEY = "orphan_guard_pending"


def _suspicious_reason(current_count: int, previous_count: int) -> str | None:
    """Return a guard-trip reason for a suspicious item-count drop, else None."""
    if current_count < _MIN_ITEMS_TO_TRUST and previous_count >= _MIN_ITEMS_TO_TRUST:
        return "below_min_items"
    if (
        previous_count > _MIN_ITEMS_FOR_RATIO_CHECK
        and current_count < previous_count * _MIN_RATIO_TO_TRUST
    ):
        return "below_ratio"
    return None


def _get_pending_guard_libs(conn: sqlite3.Connection) -> set[int]:
    """Return the set of library ids awaiting a confirming second scan."""
    raw = repository.read_setting(conn, _PENDING_GUARD_KEY)
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {int(x) for x in data}


def _set_pending_guard_libs(conn: sqlite3.Connection, libs: set[int]) -> None:
    """Persist the set of library ids awaiting a confirming second scan."""
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (_PENDING_GUARD_KEY, json.dumps(sorted(libs)), now_iso()),
    )


def _resolve_prune_libs(
    conn: sqlite3.Connection,
    scanned_libs: set[int],
    reason: str | None,
    current_count: int,
    previous_count: int,
) -> set[int]:
    """Return the subset of *scanned_libs* that may be pruned this run.

    Confirmation is PER LIBRARY: on a suspicious scan a library may be
    pruned only if it was *already* pending from the previous run — i.e. the
    second consecutive suspicious look at that specific library, exactly as
    remove_orphans' docstring promises. Libraries seeing their first
    suspicious scan are recorded as pending and deferred; they get pruned only
    if the next scan is still suspicious. A whole-set subset test would
    prune a library on its first suspicious scan merely because it was
    part of a larger previously-pending set.

    A healthy scan (``reason is None``) prunes every scanned library and clears
    any pending marker for them. Persists the updated pending set as a
    side-effect and commits it.
    """
    pending = _get_pending_guard_libs(conn)
    if reason is None:
        if pending & scanned_libs:
            # Clear the marker so a future glitch starts the cycle afresh.
            _set_pending_guard_libs(conn, pending - scanned_libs)
        return set(scanned_libs)

    confirmed = scanned_libs & pending  # already pending → second look → prune
    unconfirmed = scanned_libs - pending  # first suspicious look → defer
    # Keep markers for libs not scanned now, add the newly-suspicious
    # unconfirmed libs, and drop the confirmed libs we are about to prune.
    _set_pending_guard_libs(conn, (pending - scanned_libs) | unconfirmed)
    conn.commit()
    if unconfirmed:
        logger.warning(
            "engine.orphan_guard.skip reason=%s current=%d previous=%d "
            "unconfirmed_libs=%s — suspicious drop; awaiting a confirming "
            "second scan before removing orphans from these libraries.",
            reason,
            current_count,
            previous_count,
            sorted(unconfirmed),
        )
    if confirmed:
        logger.warning(
            "engine.orphan_guard.confirm reason=%s current=%d previous=%d "
            "confirmed_libs=%s — low item count seen on two consecutive scans; "
            "treating as a genuine deletion and removing orphans.",
            reason,
            current_count,
            previous_count,
            sorted(confirmed),
        )
    return confirmed


def remove_orphans(
    conn: sqlite3.Connection,
    seen_keys: set[str],
    scanned_libs: set[int],
    *,
    listed_since: str | None = None,
) -> int:
    """Remove ``media_items`` whose ``plex_rating_key`` is gone from Plex.

    Only considers items belonging to *scanned_libs* (libraries that were
    successfully fetched during this scan run) so items from unreachable
    libraries are never accidentally deleted.

    A suspicious item-count drop (see _suspicious_reason) — which a
    transient Plex hiccup and a genuine mass-deletion look identical on a
    single scan — is not refused outright. The first suspicious scan
    for a library records it as *pending* and skips; a second consecutive
    suspicious scan confirms the drop is real (a transient empty would have
    recovered by then) and proceeds with removal. This lets a legitimately
    shrunk library (e.g. the last show deleted) reconcile on the next scan
    instead of sticking forever, while still absorbing a one-off glitch.
    Pruning only removes mediaman's tracking rows — never media files — and a
    later healthy scan re-populates anything Plex still has, with the keeps,
    last watches and Watch progress set aside here, so a wrong prune leaves nothing unprotected.

    An item whose deletion removed files that are not booked yet stays until
    that is done: its rows are the only record of them.

    An item another run saw, or anyone scheduled or kept, after the run began
    stays too: the run's list may be from before the item came to Plex.

    Args:
        conn: Open SQLite connection.
        seen_keys: Set of ``plex_rating_key`` values observed in the scan.
        scanned_libs: Integer library IDs that were successfully fetched.
        listed_since: When the run began, before it listed anything.

    Returns:
        Number of rows deleted.
    """
    if not scanned_libs:
        return 0

    previous_count = repository.count_items_in_libraries(conn, list(scanned_libs))
    current_count = len(seen_keys)

    reason = _suspicious_reason(current_count, previous_count)
    prune_libs = _resolve_prune_libs(conn, scanned_libs, reason, current_count, previous_count)
    if not prune_libs:
        return 0

    all_ids = repository.fetch_ids_in_libraries(conn, list(prune_libs))
    unlisted = [i for i in all_ids if i not in seen_keys]
    if not unlisted:
        return 0

    # Atomic two-table delete: the matching ``scheduled_actions`` rows
    # are dropped first, then the ``media_items`` rows, both inside one
    # transaction so a crash, foreign-key violation, or concurrent
    # writer cannot leave a ``scheduled_actions`` row pointing at a
    # deleted ``media_items`` row. Each repository call chunks its own
    # IN-clause; the ``with conn:`` here owns the transaction boundary.
    with conn:
        # The lock comes before the read, so no deletion starts in between.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if listed_since is not None:
            touched = repository.fetch_ids_touched_since(conn, listed_since)
            unlisted = [i for i in unlisted if i not in touched]
        unbooked = repository.fetch_unbooked_media_ids(conn)
        repository.forget_unremoved_files(conn, unbooked.intersection(unlisted))
        orphan_ids = [i for i in unlisted if i not in unbooked]
        repository.save_pruned_keeps(conn, orphan_ids)
        repository.save_pruned_show_seasons(conn, set(orphan_ids))
        repository.save_pruned_progress(conn, set(orphan_ids))
        repository.save_pruned_watches(conn, orphan_ids)
        repository.delete_actions_for_media_items(conn, orphan_ids)
        repository.delete_media_items(conn, orphan_ids)
    if not orphan_ids:
        return 0
    logger.info(
        "Removed %d orphaned media items no longer in Plex",
        len(orphan_ids),
    )
    return len(orphan_ids)
