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
# Most recent shows and movies read for Continue watching, well above the cards a row can hold.
_CONTINUE_CANDIDATES = 200

# A row made by a mark was never played: this time sorts it behind every play, so marking an
# old episode cannot become the place to resume from.
NEVER_PLAYED = "1970-01-01T00:00:00+00:00"

# Keys per IN (...) so a large library stays under SQLite's bound-parameter limit.
_IN_CHUNK = 500

_COLUMNS = (
    "rating_key, media_item_id, show_rating_key, position_s, duration_s, watched, updated_at,"
    " seen_at"
)


@dataclass(frozen=True, slots=True)
class WatchProgress:
    rating_key: str
    media_item_id: str | None
    show_rating_key: str | None
    position_s: float
    duration_s: float
    watched: bool
    updated_at: str
    seen_at: str | None = None

    @property
    def seen(self) -> bool:
        """Finished now or once before, or marked watched by hand."""
        return self.watched or self.seen_at is not None

    @property
    def played(self) -> bool:
        """False when the row only holds a mark."""
        return self.updated_at > NEVER_PLAYED


@dataclass(frozen=True, slots=True)
class MarkTarget:
    """One movie or episode to mark watched, as its progress row would be keyed."""

    rating_key: str
    media_item_id: str
    show_rating_key: str | None
    duration_s: float


@dataclass(frozen=True, slots=True)
class MarkChange:
    """What one row was before a mark or unmark, and the seen time it left there."""

    rating_key: str
    watched: bool
    seen_at: str | None
    position_s: float
    updated_at: str
    new_seen_at: str | None


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
        seen_at=row["seen_at"],
    )


def _utc_iso(now: datetime) -> str:
    return ensure_tz(now).astimezone(UTC).isoformat()


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
    a watched item again from the start makes it in progress again; that it
    was once watched, or marked so, stays in ``seen_at``.
    """
    duration = _finite(duration_s)
    position = _finite(position_s)
    if duration > 0:
        position = min(position, duration)
    watched = duration > 0 and position >= WATCHED_FRACTION * duration
    threshold = min(CREDIT_SECONDS, WATCHED_FRACTION * duration) if duration > 0 else CREDIT_SECONDS
    now_iso = _utc_iso(now)
    credited_at = now_iso if _finite(played_s) >= threshold else None
    # with conn: a failed write rolls back, so the connection is not left holding the lock
    with conn:
        conn.execute(
            "INSERT INTO watch_progress (username, rating_key, media_item_id, show_rating_key,"
            " position_s, duration_s, watched, credited_at, updated_at, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(username, rating_key) DO UPDATE SET"
            " media_item_id = excluded.media_item_id,"
            " show_rating_key = excluded.show_rating_key,"
            " position_s = excluded.position_s,"
            " duration_s = excluded.duration_s,"
            " watched = excluded.watched,"
            " credited_at = COALESCE(excluded.credited_at, watch_progress.credited_at),"
            " updated_at = excluded.updated_at,"
            # A row watched before seen_at existed carries its watch over when played again.
            " seen_at = COALESCE(watch_progress.seen_at, excluded.seen_at,"
            " CASE WHEN watch_progress.watched = 1 THEN watch_progress.updated_at END)",
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
                now_iso if watched else None,
            ),
        )
    return watched


def mark_watched(
    conn: sqlite3.Connection, username: str, targets: Sequence[MarkTarget], now: datetime
) -> list[MarkChange]:
    """Mark *targets* watched for *username*; returns what each row it changed was before.

    Never sets ``credited_at``, nor the media item of an existing row: the
    cleanup scan reads those, and a mark is not a watch.
    """
    now_iso = _utc_iso(now)
    changes: list[MarkChange] = []
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        held = get_progress(conn, username, [t.rating_key for t in targets])
        for target in targets:
            row = held.get(target.rating_key)
            if row is None:
                conn.execute(
                    "INSERT INTO watch_progress (username, rating_key, media_item_id,"
                    " show_rating_key, position_s, duration_s, watched, updated_at, seen_at)"
                    " VALUES (?, ?, ?, ?, 0, ?, 1, ?, ?)",
                    (
                        username,
                        target.rating_key,
                        target.media_item_id,
                        target.show_rating_key,
                        _finite(target.duration_s),
                        NEVER_PLAYED,
                        now_iso,
                    ),
                )
                changes.append(
                    MarkChange(target.rating_key, False, None, 0.0, NEVER_PLAYED, now_iso)
                )
            elif not row.watched:
                seen_at = row.seen_at or now_iso
                conn.execute(
                    "UPDATE watch_progress SET watched = 1, seen_at = ?"
                    " WHERE username = ? AND rating_key = ?",
                    (seen_at, username, target.rating_key),
                )
                changes.append(_change(row, seen_at))
    return changes


def _change(row: WatchProgress, new_seen_at: str | None) -> MarkChange:
    return MarkChange(
        row.rating_key, row.watched, row.seen_at, row.position_s, row.updated_at, new_seen_at
    )


def progress_of(
    conn: sqlite3.Connection, username: str, kind: str, key: str
) -> list[WatchProgress]:
    """*username*'s progress rows of a movie, episode, season or show (without its specials)."""
    where = {
        "movie": "rating_key = ?",
        "episode": "rating_key = ?",
        "season": "media_item_id = ?",
        "show": "show_rating_key = ? AND COALESCE(media_item_id, '') NOT IN"
        " (SELECT id FROM media_items WHERE season_number = 0)",
    }[kind]
    # rationale: *where* is one of the fixed fragments above; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT {_COLUMNS} FROM watch_progress WHERE username = ? AND {where}", (username, key)
    ).fetchall()
    return [_row_to_progress(row) for row in rows]


def unmark_watched(
    conn: sqlite3.Connection, username: str, kind: str, key: str
) -> list[MarkChange]:
    """Mark a movie, episode, season or show not watched for *username*; returns what each
    row it changed was before.

    The rows stay (they carry the cleanup scan's credit); one past the
    watched mark starts over, any other keeps its place. A show's specials
    are left alone, as marking the show leaves them.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        changes = [_change(r, None) for r in progress_of(conn, username, kind, key) if r.seen]
        conn.executemany(
            "UPDATE watch_progress SET watched = 0, seen_at = NULL, position_s ="
            " CASE WHEN duration_s > 0 AND position_s >= ? * duration_s THEN 0 ELSE position_s END"
            " WHERE username = ? AND rating_key = ?",
            [(WATCHED_FRACTION, username, c.rating_key) for c in changes],
        )
    return changes


def restore_marks(conn: sqlite3.Connection, username: str, changes: Sequence[MarkChange]) -> None:
    """Put *username*'s rows back as they were before a mark or unmark.

    A row marked again since is left alone, and one played since keeps the
    place and state of that play.
    """
    with conn:
        conn.executemany(
            "UPDATE watch_progress SET seen_at = ?,"
            " watched = CASE WHEN updated_at = ? THEN ? ELSE watched END,"
            " position_s = CASE WHEN updated_at = ? THEN ? ELSE position_s END"
            " WHERE username = ? AND rating_key = ? AND seen_at IS ?",
            [
                (
                    c.seen_at,
                    c.updated_at,
                    int(c.watched),
                    c.updated_at,
                    c.position_s,
                    username,
                    c.rating_key,
                    c.new_seen_at,
                )
                for c in changes
            ],
        )


def dismiss(conn: sqlite3.Connection, username: str, title_key: str, now: datetime) -> None:
    """Hide a movie or show (by rating key) from *username*'s Continue watching until played."""
    with conn:
        conn.execute(
            "INSERT INTO watch_dismissed (username, title_key, dismissed_at) VALUES (?, ?, ?)"
            " ON CONFLICT(username, title_key) DO UPDATE SET dismissed_at = excluded.dismissed_at",
            (username, title_key, _utc_iso(now)),
        )


def undismiss(conn: sqlite3.Connection, username: str, title_key: str) -> None:
    with conn:
        conn.execute(
            "DELETE FROM watch_dismissed WHERE username = ? AND title_key = ?",
            (username, title_key),
        )


def seen_movies(conn: sqlite3.Connection, username: str) -> set[str]:
    """The rating keys of the movies *username* has seen."""
    rows = conn.execute(
        "SELECT rating_key FROM watch_progress WHERE username = ? AND show_rating_key IS NULL"
        " AND (watched = 1 OR seen_at IS NOT NULL)",
        (username,),
    )
    return {row["rating_key"] for row in rows}


def get_progress(
    conn: sqlite3.Connection, username: str, rating_keys: Sequence[str]
) -> dict[str, WatchProgress]:
    """Return *username*'s progress for each of *rating_keys* that has any, by rating key."""
    found: dict[str, WatchProgress] = {}
    for start in range(0, len(rating_keys), _IN_CHUNK):
        chunk = rating_keys[start : start + _IN_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        # rationale: IN-clause batching — only "?" placeholders interpolated; every value is bound
        rows = conn.execute(  # nosec B608
            f"SELECT {_COLUMNS} FROM watch_progress"
            f" WHERE username = ? AND rating_key IN ({placeholders})",
            (username, *chunk),
        ).fetchall()
        found.update({row["rating_key"]: _row_to_progress(row) for row in rows})
    return found


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
    watched, the card has ``find_next`` set (see ContinueCard). A title
    dismissed since it was last played is left out, and so is a show of
    which nothing was played, or nothing is seen or begun any more.
    """
    # The latest row per show (or per movie) is picked in SQL, so one long binge of a show
    # cannot push every other show and movie out of the rows read.
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM ("  # nosec B608
        f"SELECT {_COLUMNS}, COALESCE(show_rating_key, rating_key) AS title_key,"
        " MAX(watched = 1 OR seen_at IS NOT NULL OR position_s > 0) OVER ("
        "PARTITION BY COALESCE(show_rating_key, rating_key)) AS begun,"
        " ROW_NUMBER() OVER ("
        "PARTITION BY COALESCE(show_rating_key, rating_key)"
        " ORDER BY updated_at DESC, rating_key) AS rn"
        " FROM watch_progress WHERE username = ?) latest"
        " WHERE rn = 1 AND updated_at > ?"
        " AND (CASE WHEN show_rating_key IS NOT NULL THEN begun"
        " ELSE watched = 0 AND position_s > ? END)"
        " AND NOT EXISTS (SELECT 1 FROM watch_dismissed d WHERE d.username = ?"
        " AND d.title_key = latest.title_key AND d.dismissed_at >= latest.updated_at)"
        " ORDER BY updated_at DESC, rating_key LIMIT ?",
        (username, NEVER_PLAYED, MIN_RESUME_SECONDS, username, _CONTINUE_CANDIDATES),
    ).fetchall()
    cards: list[ContinueCard] = []
    for row in rows:
        progress = _row_to_progress(row)
        cards.append(
            ContinueCard(
                progress=progress,
                find_next=progress.show_rating_key is not None and progress.watched,
            )
        )
    return cards
