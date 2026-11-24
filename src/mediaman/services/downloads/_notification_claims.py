"""Claim/release mechanics for download notification rows.

Provides atomic claim, per-row and bulk release, a claim re-stamp, and a reconcile
sweep that resets rows left stuck at ``notified=2`` by a crashed worker; the claim runs
that sweep first, so a stranded row is freed on the next tick.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import timedelta

from mediaman.core.time import now_iso, now_utc

logger = logging.getLogger(__name__)

#: How long an in-flight claim is allowed before reconcile treats the row as
#: stranded by a crashed worker.  Generous enough to outlast the slowest
#: legitimate notify pipeline (Mailgun retries, slow SMTP), short enough that
#: a stranded row is recovered on the next tick after it rather than waiting
#: for the operator to notice.
STRANDED_CLAIM_GRACE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ClaimedNotificationRow:
    """A row returned by the atomic claim; fields mirror the claim's SELECT columns,
    and claimed_at is the stamp that says whose claim it is."""

    id: int
    email: str
    title: str
    media_type: str
    tmdb_id: int | None
    tvdb_id: int | None
    service: str
    claimed_at: str
    season_number: int | None = None


def _row_to_claimed(row: sqlite3.Row, claimed_at: str) -> ClaimedNotificationRow:
    return ClaimedNotificationRow(
        id=int(row["id"]),
        email=str(row["email"]),
        title=str(row["title"]),
        media_type=str(row["media_type"]),
        tmdb_id=int(row["tmdb_id"]) if row["tmdb_id"] is not None else None,
        tvdb_id=int(row["tvdb_id"]) if row["tvdb_id"] is not None else None,
        service=str(row["service"]),
        claimed_at=claimed_at,
        season_number=int(row["season_number"]) if row["season_number"] is not None else None,
    )


def _claim_pending_notifications(conn: sqlite3.Connection) -> list[ClaimedNotificationRow]:
    """Atomically claim every un-notified row with UPDATE ... WHERE notified=0 RETURNING,
    so a concurrent worker or re-entrant tick cannot claim the same row.

    Without RETURNING support (SQLite before 3.35) it falls back to SELECT-then-UPDATE
    inside an IMMEDIATE transaction so the write lock blocks any concurrent claim.
    """
    # A claim a crash left behind is freed here, every tick, not only at startup; the
    # one-hour grace keeps a send still in flight from being claimed a second time.
    reconcile_stranded_notifications(conn)
    claim_iso = now_iso()
    try:
        with conn:
            rows = conn.execute(
                "UPDATE download_notifications SET notified=2, claimed_at=? "
                "WHERE notified=0 "
                "RETURNING id, email, title, media_type, tmdb_id, tvdb_id, service, season_number",
                (claim_iso,),
            ).fetchall()
            # Build the return value before the commit so the rows are safely in a
            # local variable — a Python exception between commit and return would
            # otherwise strand them at notified=2.
            claimed = [_row_to_claimed(r, claim_iso) for r in rows]
        return claimed
    except sqlite3.OperationalError:
        # Older SQLite without RETURNING — fall back to lock-then-claim.
        # ``with conn:`` commits on normal exit and rolls back on exception;
        # BEGIN IMMEDIATE here preserves write-lock semantics so a sibling
        # worker cannot pick up the same rows concurrently.
        #
        # NOTE: this pattern is correct ONLY because db/connection.py
        # creates connections with the DEFAULT isolation_level (legacy mode),
        # meaning ``with conn:`` does NOT auto-BEGIN — the explicit
        # ``BEGIN IMMEDIATE`` below is the first and only transaction start.
        # Do NOT change connection.py to use isolation_level=None (autocommit)
        # without updating this fallback path.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, email, title, media_type, tmdb_id, tvdb_id, service, season_number "
                "FROM download_notifications WHERE notified=0"
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                # rationale: placeholder list built from integer row IDs only; no user input reaches the SQL string
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE download_notifications SET notified=2, claimed_at=? WHERE id IN ({placeholders})",
                    (claim_iso, *ids),
                )
            # Materialise the rows into a local list BEFORE exiting the
            # ``with conn:`` block (which issues the commit). The RETURNING
            # path above commits first and then returns, so the fallback must
            # match that ordering: commit-then-return, not return-inside-with.
            # A deferred commit failure in the ``with`` block would otherwise
            # cause emails to be sent for rows that were never marked notified
            # → duplicate emails on the next tick.
            claimed = [_row_to_claimed(r, claim_iso) for r in rows]
        return claimed


def _release_claim(conn: sqlite3.Connection, row: ClaimedNotificationRow) -> None:
    """Roll a claimed row back to notified=0 so a later tick can retry. Clears claimed_at
    so a reconcile sweep does not see a phantom in-flight stamp.
    """
    _release_claims_bulk(conn, [row])


def _release_claims_bulk(conn: sqlite3.Connection, rows: list[ClaimedNotificationRow]) -> None:
    """Roll claimed rows back to notified=0 in one transaction; a no-op for empty *rows*.
    Per-row release cost an fsync per row on every tick while Mailgun is unconfigured. A
    row another tick has claimed since is left to it.
    """
    if not rows:
        return
    try:
        with conn:
            conn.executemany(
                "UPDATE download_notifications SET notified=0, claimed_at=NULL "
                "WHERE id=? AND notified=2 AND claimed_at=?",
                [(row.id, row.claimed_at) for row in rows],
            )
    except sqlite3.Error:
        logger.warning("failed to release notification claims (n=%d)", len(rows), exc_info=True)


def restamp_claim(
    conn: sqlite3.Connection, row: ClaimedNotificationRow
) -> ClaimedNotificationRow | None:
    """Refresh a claimed row's claimed_at, so a slow batch does not age a row past the
    stranded-claim grace before its turn to send comes up. Returns the row with its new
    stamp, or None when the claim is no longer this one: another tick has the row.
    """
    claimed_at = now_iso()
    with conn:
        cur = conn.execute(
            "UPDATE download_notifications SET claimed_at=? "
            "WHERE id=? AND notified=2 AND claimed_at=?",
            (claimed_at, row.id, row.claimed_at),
        )
    return replace(row, claimed_at=claimed_at) if cur.rowcount else None


def reconcile_stranded_notifications(
    conn: sqlite3.Connection,
    *,
    grace_seconds: int = STRANDED_CLAIM_GRACE_SECONDS,
) -> int:
    """Reset rows stranded at notified=2 after a crashed worker.

    The claim flips notified 0 to 2 before the send, so an OOM, restart or SIGKILL in
    between pins the row; the in-process release only fires on Python exceptions. Runs at
    the start of every claim, so a stranded row is freed on the next tick. Rows whose claimed_at is older than *grace_seconds* go back to
    notified=0; returns the number reset.
    """
    cutoff = (now_utc() - timedelta(seconds=grace_seconds)).isoformat()
    with conn:
        cur = conn.execute(
            "UPDATE download_notifications "
            "SET notified=0, claimed_at=NULL "
            "WHERE notified=2 "
            "  AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
    reset = cur.rowcount or 0
    if reset:
        logger.info("notifications.reconcile reset=%d cutoff=%s", reset, cutoff)
    return reset
