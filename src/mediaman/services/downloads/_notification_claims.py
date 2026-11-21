"""Claim/release mechanics for download notification rows.

Provides atomic claim, per-row and bulk release, and a startup reconcile
sweep that resets rows left stuck at ``notified=2`` by a crashed worker.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from mediaman.core.time import now_iso, now_utc

logger = logging.getLogger(__name__)

#: How long an in-flight claim is allowed before reconcile treats the row as
#: stranded by a crashed worker.  Generous enough to outlast the slowest
#: legitimate notify pipeline (Mailgun retries, slow SMTP), short enough that
#: a stranded row is recovered on the next service restart rather than waiting
#: for the operator to notice.
STRANDED_CLAIM_GRACE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ClaimedNotificationRow:
    """A row returned by the atomic claim; fields mirror the claim's SELECT columns."""

    id: int
    email: str
    title: str
    media_type: str
    tmdb_id: int | None
    tvdb_id: int | None
    service: str


def _row_to_claimed(row: sqlite3.Row) -> ClaimedNotificationRow:
    return ClaimedNotificationRow(
        id=int(row["id"]),
        email=str(row["email"]),
        title=str(row["title"]),
        media_type=str(row["media_type"]),
        tmdb_id=int(row["tmdb_id"]) if row["tmdb_id"] is not None else None,
        tvdb_id=int(row["tvdb_id"]) if row["tvdb_id"] is not None else None,
        service=str(row["service"]),
    )


def _claim_pending_notifications(conn: sqlite3.Connection) -> list[ClaimedNotificationRow]:
    """Atomically claim every un-notified row with UPDATE ... WHERE notified=0 RETURNING,
    so a concurrent worker or re-entrant tick cannot claim the same row.

    Without RETURNING support (SQLite before 3.35) it falls back to SELECT-then-UPDATE
    inside an IMMEDIATE transaction so the write lock blocks any concurrent claim.
    """
    claim_iso = now_iso()
    try:
        rows = conn.execute(
            "UPDATE download_notifications SET notified=2, claimed_at=? "
            "WHERE notified=0 "
            "RETURNING id, email, title, media_type, tmdb_id, tvdb_id, service",
            (claim_iso,),
        ).fetchall()
        # Build the return value before committing so the rows are safely in a
        # local variable — a Python exception between commit and return would
        # otherwise strand them at notified=2.
        claimed = [_row_to_claimed(r) for r in rows]
        conn.commit()
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
                "SELECT id, email, title, media_type, tmdb_id, tvdb_id, service "
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
            claimed = [_row_to_claimed(r) for r in rows]
        return claimed


def _release_claim(conn: sqlite3.Connection, row_id: int) -> None:
    """Roll a claimed row back to notified=0 so a later tick can retry. Clears claimed_at
    so a reconcile sweep does not see a phantom in-flight stamp.
    """
    try:
        conn.execute(
            "UPDATE download_notifications SET notified=0, claimed_at=NULL WHERE id=?",
            (row_id,),
        )
        conn.commit()
    except sqlite3.Error:
        logger.warning("failed to release notification claim id=%s", row_id, exc_info=True)


def _release_claims_bulk(conn: sqlite3.Connection, row_ids: list[int]) -> None:
    """Roll many claimed rows back to notified=0 with one UPDATE and one commit; a no-op
    for empty *row_ids*. Per-row release cost an fsync per row on every tick while
    Mailgun is unconfigured.
    """
    if not row_ids:
        return
    try:
        # rationale: placeholder list built from integer row IDs only; no user input reaches the SQL string
        placeholders = ",".join("?" * len(row_ids))
        conn.execute(
            f"UPDATE download_notifications SET notified=0, claimed_at=NULL "
            f"WHERE id IN ({placeholders})",
            row_ids,
        )
        conn.commit()
    except sqlite3.Error:
        logger.warning(
            "failed to bulk-release notification claims (n=%d)", len(row_ids), exc_info=True
        )


def reconcile_stranded_notifications(
    conn: sqlite3.Connection,
    *,
    grace_seconds: int = STRANDED_CLAIM_GRACE_SECONDS,
) -> int:
    """Reset rows stranded at notified=2 after a crashed worker.

    The claim flips notified 0 to 2 before the send, so an OOM, restart or SIGKILL in
    between pins the row; the in-process release only fires on Python exceptions. Call
    once on startup. Rows whose claimed_at is older than *grace_seconds* go back to
    notified=0; returns the number reset.
    """
    cutoff = (now_utc() - timedelta(seconds=grace_seconds)).isoformat()
    cur = conn.execute(
        "UPDATE download_notifications "
        "SET notified=0, claimed_at=NULL "
        "WHERE notified=2 "
        "  AND (claimed_at IS NULL OR claimed_at < ?)",
        (cutoff,),
    )
    conn.commit()
    reset = cur.rowcount or 0
    if reset:
        logger.info("notifications.reconcile reset=%d cutoff=%s", reset, cutoff)
    return reset
