"""What stands against asking for a season of a show Sonarr tracks, and the record of one asked for."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from mediaman.core.audit import log_audit
from mediaman.core.deleted_media import DeletedMedia, redownload_tokens
from mediaman.services.downloads.notifications import record_download_notification

Refusal = Literal["leaving", "removed"]

# The seasons of a show by any id Plex gave it, or by its name when it gave none.
_OF_SHOW = (
    "season_number IS NOT NULL AND (tvdb_id = :tvdb OR tmdb_id = :tmdb"
    " OR last_tvdb_id = :tvdb OR last_tmdb_id = :tmdb"
    " OR (COALESCE(tvdb_id, tmdb_id, last_tvdb_id, last_tmdb_id) IS NULL"
    " AND show_title = :title COLLATE NOCASE))"
)


@dataclass(frozen=True, slots=True)
class ShowIds:
    tmdb_id: int | None
    tvdb_id: int | None
    title: str

    def params(self) -> dict[str, int | str | None]:
        return {"tmdb": self.tmdb_id, "tvdb": self.tvdb_id, "title": self.title}


def library_seasons(conn: sqlite3.Connection, show: ShowIds) -> set[int]:
    """The seasons of the show the last scans found in Plex."""
    # rationale: the interpolated condition is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT season_number FROM media_items WHERE {_OF_SHOW}", show.params()
    )
    return {row[0] for row in rows}


def season_refusals(
    conn: sqlite3.Connection, show: ShowIds, numbers: set[int]
) -> dict[int, Refusal]:
    """Which of the seasons *numbers* stand in the way of being asked for, and how.

    One "leaving" is scheduled for deletion, or being deleted, and would be fetched only to go.
    One "removed" mediaman deleted, and nobody downloaded it again since: it is for an admin to
    bring back, as Re-download is.
    """
    leaving = _leaving(conn, show)
    removed = _removed(conn, show, numbers)
    refused: dict[int, Refusal] = dict.fromkeys(removed, "removed")
    refused.update(dict.fromkeys(leaving & numbers, "leaving"))
    return refused


def _leaving(conn: sqlite3.Connection, show: ShowIds) -> set[int]:
    # rationale: the interpolated condition is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT season_number FROM media_items mi WHERE {_OF_SHOW} AND ("
        "EXISTS (SELECT 1 FROM scheduled_actions sa WHERE sa.media_item_id = mi.id"
        " AND sa.action = 'scheduled_deletion' AND sa.token_used = 0"
        " AND (sa.delete_status IS NULL OR sa.delete_status IN ('pending', 'deleting')))"
        " OR EXISTS (SELECT 1 FROM delete_intents di WHERE di.media_item_id = mi.id"
        " AND di.completed_at IS NULL AND di.last_error IS NULL))",
        show.params(),
    )
    return {row[0] for row in rows}


def _removed(conn: sqlite3.Connection, show: ShowIds, numbers: set[int]) -> set[int]:
    """The seasons mediaman deleted that nobody downloaded again since."""
    deletions = conn.execute(
        "SELECT season_number, MAX(audit_id) FROM deleted_media WHERE media_type != 'movie'"
        " AND (tvdb_id = :tvdb OR tmdb_id = :tmdb"
        " OR (tvdb_id IS NULL AND tmdb_id IS NULL AND title = :title COLLATE NOCASE))"
        " GROUP BY season_number",
        show.params(),
    ).fetchall()
    latest = {row[0]: row[1] for row in deletions}
    # One recorded without a season was of any of them.
    of_any = latest.get(None, 0)
    return {
        number
        for number in numbers
        if (audit_id := max(latest.get(number, 0), of_any))
        and not _downloaded_since(conn, show, number, audit_id)
    }


def _downloaded_since(conn: sqlite3.Connection, show: ShowIds, number: int, audit_id: int) -> bool:
    tokens = redownload_tokens(
        DeletedMedia(audit_id, "tv_season", show.tmdb_id, show.tvdb_id, number, show.title)
    )
    # rationale: only "?" placeholders are interpolated; every value is bound
    found = conn.execute(  # nosec B608
        "SELECT 1 FROM audit_log WHERE id > ? AND action IN ('downloaded', 're_downloaded')"
        f" AND media_item_id IN ({','.join('?' * len(tokens))}) LIMIT 1",
        (audit_id, *tokens),
    ).fetchone()
    return found is not None


def record_season(
    conn: sqlite3.Connection,
    *,
    title: str,
    tmdb_id: int,
    tvdb_id: int,
    number: int,
    actor: str,
    email: str | None,
    added: bool,
) -> None:
    """Record the season *actor* asked for: its mail, and the download when they *added* it.

    Named by its show's id and its number, as a re-download is, so a deletion of another
    season of the show is not taken for one asked for again.
    """
    with conn:
        if added:
            log_audit(
                conn,
                f"tvdb:{tvdb_id}:s{number}",
                "downloaded",
                f"Added season {number} of '{title}' to Sonarr by {actor}",
                actor=actor,
            )
        if email is not None:
            record_download_notification(
                conn,
                email=email,
                title=title,
                media_type="tv",
                tmdb_id=tmdb_id,
                tvdb_id=tvdb_id,
                service="sonarr",
                season_number=number,
                username=actor,
            )
