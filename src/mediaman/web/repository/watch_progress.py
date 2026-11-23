"""Repository functions for each person's Watch progress.

Progress lives only in mediaman's ``watch_progress`` table; nothing here
talks to Plex. Every read is scoped to one username.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from mediaman.core.format import ensure_tz

# Fraction of the duration past which an item counts as watched.
WATCHED_FRACTION = 0.9
# Seconds actually played before a play counts as a watch for cleanup;
# shorter items need to be played to the watched mark instead.
CREDIT_SECONDS = 300.0
# A movie needs more than this many seconds in to show in Continue watching.
MIN_RESUME_SECONDS = 30.0

_COLUMNS = "rating_key, media_item_id, show_rating_key, position_s, duration_s, watched, updated_at"


@dataclass(frozen=True, slots=True)
class WatchProgress:
    rating_key: str
    media_item_id: str | None
    show_rating_key: str | None
    position_s: float
    duration_s: float
    watched: bool
    updated_at: str


@dataclass(frozen=True, slots=True)
class ContinueCard:
    """One Continue watching card: a movie, or the last-played episode of a show.

    When *find_next* is true the show's last-played episode
    (``progress.rating_key``) is finished, and the caller must look up the
    next unwatched episode after it; if there is none the show drops out.
    """

    progress: WatchProgress
    find_next: bool


def _finite(value: float) -> float:
    """Return *value* as a non-negative finite float (NaN, infinities and negatives become 0)."""
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def _row_to_progress(row: sqlite3.Row) -> WatchProgress:
    return WatchProgress(
        rating_key=row["rating_key"],
        media_item_id=row["media_item_id"],
        show_rating_key=row["show_rating_key"],
        position_s=row["position_s"],
        duration_s=row["duration_s"],
        watched=bool(row["watched"]),
        updated_at=row["updated_at"],
    )


def save_progress(
    conn: sqlite3.Connection,
    *,
    username: str,
    rating_key: str,
    media_item_id: str | None,
    show_rating_key: str | None,
    position_s: float,
    duration_s: float,
    played_s: float,
    now: datetime,
) -> bool:
    """Store *username*'s position in *rating_key* and return whether it is watched.

    *played_s* is the time actually played on the page so far. Once it
    reaches five minutes (or the watched mark of a shorter item) the row's
    ``credited_at`` is set to *now*, which the cleanup scan reads as a
    watch. The watched flag follows the position on every save, so playing
    a watched item again from the start makes it in progress again.
    """
    duration = _finite(duration_s)
    position = _finite(position_s)
    if duration > 0:
        position = min(position, duration)
    watched = duration > 0 and position >= WATCHED_FRACTION * duration
    threshold = min(CREDIT_SECONDS, WATCHED_FRACTION * duration) if duration > 0 else CREDIT_SECONDS
    now_iso = ensure_tz(now).astimezone(UTC).isoformat()
    credited_at = now_iso if _finite(played_s) >= threshold else None
    conn.execute(
        "INSERT INTO watch_progress (username, rating_key, media_item_id, show_rating_key,"
        " position_s, duration_s, watched, credited_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(username, rating_key) DO UPDATE SET"
        " media_item_id = excluded.media_item_id,"
        " show_rating_key = excluded.show_rating_key,"
        " position_s = excluded.position_s,"
        " duration_s = excluded.duration_s,"
        " watched = excluded.watched,"
        " credited_at = COALESCE(excluded.credited_at, watch_progress.credited_at),"
        " updated_at = excluded.updated_at",
        (
            username,
            rating_key,
            media_item_id,
            show_rating_key,
            position,
            duration,
            int(watched),
            credited_at,
            now_iso,
        ),
    )
    conn.commit()
    return watched


def get_progress(
    conn: sqlite3.Connection, username: str, rating_keys: Sequence[str]
) -> dict[str, WatchProgress]:
    """Return *username*'s progress for each of *rating_keys* that has any, by rating key."""
    if not rating_keys:
        return {}
    placeholders = ",".join("?" * len(rating_keys))
    # rationale: IN-clause batching — only "?" placeholders interpolated; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT {_COLUMNS} FROM watch_progress"
        f" WHERE username = ? AND rating_key IN ({placeholders})",
        (username, *rating_keys),
    ).fetchall()
    return {row["rating_key"]: _row_to_progress(row) for row in rows}


def show_progress(
    conn: sqlite3.Connection, username: str, show_rating_key: str
) -> dict[str, WatchProgress]:
    """Return *username*'s progress on every episode of one show, by episode rating key."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM watch_progress WHERE username = ? AND show_rating_key = ?",  # nosec B608
        (username, show_rating_key),
    ).fetchall()
    return {row["rating_key"]: _row_to_progress(row) for row in rows}


def continue_watching(conn: sqlite3.Connection, username: str) -> list[ContinueCard]:
    """Return *username*'s Continue watching cards, most recently played first.

    One card per movie or show. A movie appears while it is unwatched and
    more than MIN_RESUME_SECONDS in. A show is represented by its most
    recently played episode: unwatched, it is the episode to resume;
    watched, the card has ``find_next`` set (see ContinueCard).
    """
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM watch_progress WHERE username = ?"  # nosec B608
        " ORDER BY updated_at DESC, rating_key",
        (username,),
    ).fetchall()
    cards: list[ContinueCard] = []
    seen_shows: set[str] = set()
    for row in rows:
        progress = _row_to_progress(row)
        show_key = progress.show_rating_key
        if show_key is None:
            if not progress.watched and progress.position_s > MIN_RESUME_SECONDS:
                cards.append(ContinueCard(progress=progress, find_next=False))
            continue
        if show_key in seen_shows:
            continue
        seen_shows.add(show_key)
        cards.append(ContinueCard(progress=progress, find_next=progress.watched))
    return cards
