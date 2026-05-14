"""Claim/release mechanics for download notification rows.

Provides atomic claim, per-row and bulk release, and a startup reconcile
sweep that resets rows left stuck at ``notified=2`` by a crashed worker.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from mediaman.core.time import now_iso, now_utc

logger = logging.getLogger(__name__)

#: How long an in-flight claim is allowed before reconcile treats the row as
#: stranded by a crashed worker.  Generous enough to outlast the slowest
#: legitimate notify pipeline (Mailgun retries, slow SMTP), short enough that
#: a stranded row is recovered on the next service restart rather than waiting
#: for the operator to notice.
STRANDED_CLAIM_GRACE_SECONDS = 3600


def _claim_pending_notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Atomically claim every un-notified notification row.

    Uses ``UPDATE ... WHERE notified=0 RETURNING`` so a sibling worker
    (or a re-entrant scheduler tick) cannot pick up the same row a second
    time. SQLite has supported the RETURNING clause since 3.35, which is
    comfortably older than the project's ``sqlite3`` floor.

    Returns the claimed rows in the same shape the previous SELECT
    returned, so the caller's row-handling code stays unchanged. On a
    SQLite build without RETURNING we fall back to the old
    SELECT-then-UPDATE flow inside an IMMEDIATE transaction so the
    write lock blocks any concurrent claim.
    """
    claim_iso = now_iso()
    try:
        rows = conn.execute(
            "UPDATE download_notifications SET notified=2, claimed_at=? "
            "WHERE notified=0 "
            "RETURNING id, email, title, media_type, tmdb_id, tvdb_id, service",
            (claim_iso,),
        ).fetchall()
        conn.commit()
        return rows
    except sqlite3.OperationalError:
        # Older SQLite without RETURNING — fall back to lock-then-claim.
        # ``with conn:`` commits on normal exit and rolls back on exception;
        # BEGIN IMMEDIATE here preserves write-lock semantics so a sibling
        # worker cannot pick up the same rows concurrently.
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
            return rows


def _release_claim(conn: sqlite3.Connection, row_id: int) -> None:
    """Roll a claimed row back to ``notified=0`` so a future tick can retry.

    Used when the early-bail conditions inside :func:`check_download_notifications`
    fail (e.g. Mailgun later turns out to be unreachable for a specific
    item) — without this the row would stay stuck at ``notified=2``
    indefinitely.

    Clears ``claimed_at`` along with the status so a subsequent reconcile
    sweep does not see a phantom in-flight stamp on a row that is queued.
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
    """Roll many claimed rows back to ``notified=0`` in a single statement.

    The per-row :func:`_release_claim` ran one ``UPDATE`` + one
    ``COMMIT`` per stranded row.  When Mailgun is unconfigured every
    pending row goes through the release path on every tick — that's N
    fsyncs per scheduler poke. This helper does the same work in a
    single statement and a single commit.

    Skips silently when ``row_ids`` is empty so callers can pipe the
    "claimed" list straight in.
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
    """Reset rows stranded at ``notified=2`` after a crashed worker.

    The atomic claim prevents two workers from sending the same
    notification, but it does so by flipping ``notified=0 → 2`` *before*
    the actual mail attempt.  An OOM, container restart, or SIGKILL
    between the claim and the send leaves rows pinned at ``notified=2``
    forever — the in-process release path inside the sender loop only fires
    on Python exceptions.

    Call this once on startup (the FastAPI lifespan does so).  Rows whose
    ``claimed_at`` is older than *grace_seconds* are reset back to
    ``notified=0`` with ``claimed_at`` cleared so the next scheduler tick
    picks them up.  Returns the number of rows reset.

    *grace_seconds* is generous enough that a legitimate slow Mailgun
    pipeline isn't reaped — it is only ever observed by the next process
    after a restart, by which point the previous in-flight call is gone.
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
