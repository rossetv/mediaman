"""SQL operations on the `media_items` table."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from mediaman.core.format import ensure_tz as _ensure_tz
from mediaman.core.time import now_iso
from mediaman.core.time import parse_iso_utc as _parse_iso_utc

logger = logging.getLogger(__name__)


def upsert_media_item(
    conn: sqlite3.Connection,
    *,
    item: dict,
    library_id: str,
    media_type: str,
    arr_date: str | None,
) -> None:
    """Insert or update a media item record.

    Uses *arr_date* (from Radarr/Sonarr) when available, else falls back
    to Plex's ``addedAt``. The ``added_at`` column is always updated to
    reflect the best known date.
    """
    now = now_iso()

    added_at: str
    if arr_date:
        parsed = _parse_iso_utc(arr_date)
        added_at = parsed.isoformat() if parsed else arr_date
    else:
        raw = item.get("added_at")
        if isinstance(raw, datetime):
            added_at = _ensure_tz(raw).isoformat()
        elif raw is None:
            added_at = now
        else:
            added_at = str(raw)

    conn.execute(
        """
        INSERT INTO media_items (
            id, title, media_type, show_title, season_number,
            plex_library_id, plex_rating_key, show_rating_key,
            added_at, file_path, file_size_bytes, poster_path, last_scanned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            media_type = excluded.media_type,
            show_rating_key = excluded.show_rating_key,
            added_at = excluded.added_at,
            file_path = excluded.file_path,
            file_size_bytes = excluded.file_size_bytes,
            poster_path = excluded.poster_path,
            last_scanned_at = excluded.last_scanned_at
        """,
        (
            item["plex_rating_key"],
            item["title"],
            media_type,
            item.get("show_title"),
            item.get("season_number"),
            int(library_id) if str(library_id).isdigit() else library_id,
            item["plex_rating_key"],
            item.get("show_rating_key"),
            added_at,
            item.get("file_path", ""),
            item.get("file_size_bytes", 0),
            item.get("poster_path"),
            now,
        ),
    )


def update_last_watched(conn: sqlite3.Connection, media_id: str, watch_history: list[dict]) -> None:
    """Store the most recent watch timestamp for a media item.

    Monotonic: the stored ``last_watched_at`` is only advanced, never
    rewound. Plex's per-item watch history is paginated and does not
    always return the full archive on every scan — a re-scan that fetches
    only an older slice would otherwise drag the timestamp backwards
    (Domain 05 finding) and re-qualify the item for deletion. We compare
    in SQL via ``MAX(...)`` so the guard is atomic with the write.
    """
    if not watch_history:
        return
    latest = max(
        (h["viewed_at"] for h in watch_history if h.get("viewed_at")),
        default=None,
    )
    if latest is None:
        return
    latest = _ensure_tz(latest)
    latest_iso = latest.isoformat()
    # Use MAX(...) so we never rewind: if the existing value is later
    # (or equal) the column is left unchanged; NULL is treated as
    # ``-infinity`` via COALESCE so a first write always sticks.
    conn.execute(
        "UPDATE media_items "
        "SET last_watched_at = MAX(?, COALESCE(last_watched_at, '')) "
        "WHERE id = ?",
        (latest_iso, media_id),
    )


def count_items_in_libraries(conn: sqlite3.Connection, library_ids: list[int]) -> int:
    """Return the total number of ``media_items`` in *library_ids*."""
    if not library_ids:
        return 0
    lp = ",".join("?" * len(library_ids))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM media_items WHERE plex_library_id IN ({lp})",
        tuple(library_ids),
    ).fetchone()
    return row["n"] if row else 0


def fetch_ids_in_libraries(conn: sqlite3.Connection, library_ids: list[int]) -> list[str]:
    """Return every ``media_items.id`` belonging to *library_ids*.

    Chunks into groups of 500 to stay below SQLite's parameter limit.
    """
    ids: list[str] = []
    for start in range(0, len(library_ids), 500):
        chunk = library_ids[start : start + 500]
        lp = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id FROM media_items WHERE plex_library_id IN ({lp})",
            tuple(chunk),
        ).fetchall()
        ids.extend(r["id"] for r in rows)
    return ids


def delete_media_items(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Delete ``media_items`` rows and their ``scheduled_actions`` in chunks.

    Each chunk's two DELETEs run inside a ``BEGIN IMMEDIATE`` so that a
    process crash, foreign-key violation, or concurrent writer cannot
    leave the DB with ``scheduled_actions`` rows pointing at a deleted
    ``media_items`` row (or vice versa). Without the explicit
    transaction the two ``conn.execute`` calls would be split across
    SQLite's autocommit boundary, opening a window where a crash
    between them yields exactly the orphan we're trying to avoid.
    """
    if not ids:
        return
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        # If the caller already opened a transaction (the scanner
        # frequently does for the wider scan), BEGIN IMMEDIATE will
        # raise OperationalError — fall back to the existing in-flight
        # transaction in that case so we still run as one atomic
        # block, just under the caller's transaction scope.
        in_outer_txn = False
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            in_outer_txn = True
        try:
            conn.execute(
                f"DELETE FROM scheduled_actions WHERE media_item_id IN ({placeholders})",
                tuple(chunk),
            )
            conn.execute(
                f"DELETE FROM media_items WHERE id IN ({placeholders})",
                tuple(chunk),
            )
            if not in_outer_txn:
                conn.execute("COMMIT")
        except sqlite3.Error:
            if not in_outer_txn:
                conn.execute("ROLLBACK")
            raise
