"""Repository functions for the library JSON API routes.

Covers SQL operations driving the keep, delete, and redownload flows in
:mod:`mediaman.web.routes.library_api`. Tables touched here:
``media_items``, ``scheduled_actions``, ``audit_log``, and
``download_notifications`` (via :func:`record_download_notification`).

Repository purity contract: route handlers must orchestrate, not query
(`CODE_GUIDELINES.md` §2.7.1). Multi-statement work runs inside a single
``with conn:`` block so the audit row and the business mutation land in
the same commit (§9.7).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from mediaman.core.audit import log_audit
from mediaman.services.downloads.notifications import record_download_notification


class NotFound(Exception):
    """Raised when a media row that the route expected to read is absent.

    The keep and delete handlers map this onto their respective wire
    responses (404 for keep, 403 for delete to avoid leaking existence).
    Defined here rather than at the call site so the SQL function can
    raise inside ``with conn:`` and the route does not need a generic
    ``RuntimeError`` sentinel (`CODE_GUIDELINES.md` §6.1).
    """


@dataclass(frozen=True)
class MediaDeleteSnapshot:
    """Captured media_items row used to drive the delete flow.

    The snapshot is taken under a write-locked transaction so the external
    Radarr/Sonarr call can run without holding a SQLite lock. Every
    identifier the cleanup transaction needs is denormalised here so the
    delete path never re-reads the row after the lock has been released.
    """

    title: str
    media_type: str
    file_path: str | None
    file_size_bytes: int | None
    radarr_id: int | None
    sonarr_id: int | None
    season_number: int | None
    plex_rating_key: str | None


# ---------------------------------------------------------------------------
# Keep flow
# ---------------------------------------------------------------------------


def apply_keep_in_tx(
    conn: sqlite3.Connection,
    *,
    media_id: str,
    action: str,
    execute_at: str | None,
    now_iso: str,
    snooze_label: str,
    new_token: str,
    audit_detail: str,
    actor: str,
) -> None:
    """Apply a keep / snooze decision atomically with an audit row.

    Looks for an existing ``scheduled_actions`` row for *media_id* with
    ``token_used = 0`` and updates it in place if present; otherwise
    inserts a fresh row.  The audit row is written inside the same
    transaction so a SQLite failure rolls the keep back along with the
    audit record (`CODE_GUIDELINES.md` §9.7).

    Raises:
        NotFound: when the target media_items row does not exist. The
            ``with conn:`` block rolls back any partial writes.
    """
    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        media_row = conn.execute("SELECT id FROM media_items WHERE id = ?", (media_id,)).fetchone()
        if media_row is None:
            raise NotFound(media_id)

        existing = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id = ? AND token_used = 0",
            (media_id,),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE scheduled_actions "
                "SET action=?, execute_at=?, snoozed_at=?, snooze_duration=?, token_used=0 "
                "WHERE id=?",
                (action, execute_at, now_iso, snooze_label, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO scheduled_actions "
                "(media_item_id, action, scheduled_at, execute_at, token, token_used, "
                "snoozed_at, snooze_duration) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    media_id,
                    action,
                    now_iso,
                    execute_at,
                    new_token,
                    now_iso,
                    snooze_label,
                ),
            )

        log_audit(
            conn,
            media_id,
            "snoozed",
            audit_detail,
            actor=actor,
        )


# ---------------------------------------------------------------------------
# Delete flow
# ---------------------------------------------------------------------------


def snapshot_media_for_delete(conn: sqlite3.Connection, media_id: str) -> MediaDeleteSnapshot:
    """Read the media row needed for a delete and release the write-lock.

    Runs under ``BEGIN IMMEDIATE`` so concurrent writers cannot mutate the
    row between read and release; ``with conn:`` commits on normal exit
    (or rolls back on :exc:`NotFound`).

    Raises:
        NotFound: when no row matches *media_id*.
    """
    # Snapshot transaction: ``with conn:`` commits on normal exit and
    # rolls back on exception; BEGIN IMMEDIATE preserves write-lock
    # semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, title, media_type, file_path, file_size_bytes, "
            "radarr_id, sonarr_id, season_number, plex_rating_key "
            "FROM media_items WHERE id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            raise NotFound(media_id)
        return MediaDeleteSnapshot(
            title=row["title"],
            media_type=row["media_type"],
            file_path=row["file_path"],
            file_size_bytes=row["file_size_bytes"],
            radarr_id=row["radarr_id"],
            sonarr_id=row["sonarr_id"],
            season_number=row["season_number"],
            plex_rating_key=row["plex_rating_key"],
        )


def finalise_delete_in_tx(
    conn: sqlite3.Connection,
    *,
    media_id: str,
    audit_detail: str,
    space_bytes: int | None,
    actor: str,
) -> None:
    """Run the cleanup transaction: audit, prune scheduled_actions, drop row.

    Called after the external Radarr/Sonarr round-trip succeeds (or is
    detected as already-gone). Audit, scheduled_actions prune, and the
    media_items delete all land in the same commit so a SQLite failure
    rolls the lot back.
    """
    # Cleanup transaction: ``with conn:`` commits on normal exit and
    # rolls back on exception; BEGIN IMMEDIATE preserves write-lock
    # semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        log_audit(
            conn,
            media_id,
            "deleted",
            audit_detail,
            space_bytes=space_bytes,
            actor=actor,
        )
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_id,))
        conn.execute("DELETE FROM media_items WHERE id = ?", (media_id,))


# ---------------------------------------------------------------------------
# Redownload flow
# ---------------------------------------------------------------------------


def record_redownload(
    conn: sqlite3.Connection,
    *,
    audit_id: str,
    audit_detail: str,
    actor: str,
    email: str,
    title: str,
    media_type: str,
    service: str,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
) -> None:
    """Persist the audit row and download-notification claim for a redownload.

    Audit and notification rows are written under a single commit so the
    user-facing "added to Radarr/Sonarr" response is never returned with
    only half of the bookkeeping landed.
    """
    log_audit(
        conn,
        audit_id,
        "re_downloaded",
        audit_detail,
        actor=actor,
    )
    record_download_notification(
        conn,
        email=email,
        title=title,
        media_type=media_type,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        service=service,
    )
    conn.commit()
