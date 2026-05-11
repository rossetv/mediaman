"""Repository functions for dashboard reads.

Provides the SQL surface for the dashboard route.  All reads sit against
``scheduled_actions``, ``media_items`` and ``audit_log``; the route layer
in :mod:`mediaman.web.routes.dashboard._data` keeps the view-model shaping
(formatting, poster URL synthesis, type-badge mapping) and depends on
these dataclasses rather than on raw rows (§9.5).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledDeletionRow:
    """A scheduled_actions row joined with media_items for the dashboard."""

    sa_id: int
    media_item_id: str
    execute_at: str | None
    title: str
    media_type: str
    show_title: str | None
    season_number: int | None
    plex_rating_key: str | None
    added_at: str | None
    file_size_bytes: int


@dataclass(frozen=True)
class RedownloadAuditRow:
    """An audit_log row used to detect re-downloads after a deletion."""

    media_item_id: str
    created_at: str


@dataclass(frozen=True)
class DeletedAuditRow:
    """A ``deleted`` audit_log row joined with media_items for the dashboard."""

    audit_id: int
    media_item_id: str
    created_at: str
    detail: str | None
    space_reclaimed_bytes: int
    title: str | None
    media_type: str | None
    plex_rating_key: str | None


@dataclass(frozen=True)
class MediaTypeSize:
    """A (media_type, total_bytes) row from the per-type size aggregate."""

    media_type: str
    total: int


def fetch_scheduled_deletions(
    conn: sqlite3.Connection,
    deletion_action: str,
) -> list[ScheduledDeletionRow]:
    """Return scheduled-deletion rows joined with media_items, sorted by execute_at."""
    rows = conn.execute(
        """
        SELECT
            sa.id          AS sa_id,
            sa.media_item_id,
            sa.execute_at,
            mi.title,
            mi.media_type,
            mi.show_title,
            mi.season_number,
            mi.plex_rating_key,
            mi.added_at,
            mi.file_size_bytes
        FROM scheduled_actions sa
        JOIN media_items mi ON mi.id = sa.media_item_id
        WHERE sa.action = ?
          AND sa.token_used = 0
        ORDER BY sa.execute_at ASC
        """,
        (deletion_action,),
    ).fetchall()
    return [
        ScheduledDeletionRow(
            sa_id=r["sa_id"],
            media_item_id=r["media_item_id"],
            execute_at=r["execute_at"],
            title=r["title"],
            media_type=r["media_type"] or "movie",
            show_title=r["show_title"],
            season_number=r["season_number"],
            plex_rating_key=r["plex_rating_key"],
            added_at=r["added_at"],
            file_size_bytes=r["file_size_bytes"] or 0,
        )
        for r in rows
    ]


def fetch_redownload_audit_rows(conn: sqlite3.Connection) -> list[RedownloadAuditRow]:
    """Return audit_log entries for ``re_downloaded`` and ``downloaded`` actions."""
    rows = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded')"
    ).fetchall()
    return [
        RedownloadAuditRow(
            media_item_id=r["media_item_id"] or "",
            created_at=r["created_at"],
        )
        for r in rows
    ]


def fetch_deleted_audit_batch(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
) -> list[DeletedAuditRow]:
    """Return a batch of ``deleted`` audit_log rows joined with media_items."""
    rows = conn.execute(
        """
        SELECT
            al.id,
            al.media_item_id,
            al.created_at,
            al.detail,
            al.space_reclaimed_bytes,
            mi.title,
            mi.media_type,
            mi.plex_rating_key
        FROM audit_log al
        LEFT JOIN media_items mi ON mi.id = al.media_item_id
        WHERE al.action = 'deleted'
        ORDER BY al.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [
        DeletedAuditRow(
            audit_id=r["id"],
            media_item_id=r["media_item_id"] or "",
            created_at=r["created_at"],
            detail=r["detail"],
            space_reclaimed_bytes=r["space_reclaimed_bytes"] or 0,
            title=r["title"],
            media_type=r["media_type"],
            plex_rating_key=r["plex_rating_key"],
        )
        for r in rows
    ]


def fetch_media_type_sizes(conn: sqlite3.Connection) -> list[MediaTypeSize]:
    """Return the total file_size_bytes grouped by media_type."""
    rows = conn.execute(
        """
        SELECT media_type, SUM(file_size_bytes) AS total
        FROM media_items
        GROUP BY media_type
        """
    ).fetchall()
    return [MediaTypeSize(media_type=r["media_type"], total=int(r["total"] or 0)) for r in rows]


def sum_reclaimed_bytes(conn: sqlite3.Connection) -> int:
    """Return the total space_reclaimed_bytes from ``deleted`` audit_log rows."""
    row = conn.execute(
        "SELECT SUM(space_reclaimed_bytes) AS total FROM audit_log WHERE action='deleted'"
    ).fetchone()
    return int(row["total"] or 0)


@dataclass(frozen=True)
class ReclaimedWeek:
    """Aggregate reclaimed-bytes-per-week row used by the chart endpoint."""

    week: str
    reclaimed_bytes: int


def fetch_reclaimed_chart(conn: sqlite3.Connection, *, limit: int) -> list[ReclaimedWeek]:
    """Return the most recent N weeks of reclaimed-bytes totals, newest first."""
    rows = conn.execute(
        """
        SELECT
            strftime('%Y-W%W', created_at) AS week,
            SUM(space_reclaimed_bytes)     AS reclaimed_bytes
        FROM audit_log
        WHERE action = 'deleted'
          AND space_reclaimed_bytes IS NOT NULL
        GROUP BY week
        ORDER BY week DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        ReclaimedWeek(week=r["week"], reclaimed_bytes=int(r["reclaimed_bytes"] or 0)) for r in rows
    ]


__all__ = [
    "DeletedAuditRow",
    "MediaTypeSize",
    "ReclaimedWeek",
    "RedownloadAuditRow",
    "ScheduledDeletionRow",
    "fetch_deleted_audit_batch",
    "fetch_media_type_sizes",
    "fetch_reclaimed_chart",
    "fetch_redownload_audit_rows",
    "fetch_scheduled_deletions",
    "sum_reclaimed_bytes",
]
