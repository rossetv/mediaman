"""Reads and writes of ``user_picks`` and ``user_pick_runs``: each person's own picks.

Every read and write of a pick names the username it belongs to: another person's pick
does not exist here, whatever the role of who asks.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from mediaman.core.time import parse_iso_strict_utc
from mediaman.services.openai.recommendations.repository import SuggestionDetail

#: Days a person waits between refreshes of their own picks.
REFRESH_COOLDOWN_DAYS = 7
#: Refreshes people may start in 24 hours, all together and each, failed ones included.
REFRESHES_PER_DAY = 10
TRIES_PER_DAY = 3
# A run left unfinished this long ago died with its process.
_RUN_STALE = timedelta(minutes=30)
_KEEP_DAYS = 90

_COLS = (
    "id, title, year, media_type, 'personal' AS category, tmdb_id, description, reason, "
    "poster_url, trailer_url, rating, rt_rating, tagline, runtime, genres, cast_json, "
    "director, trailer_key, imdb_rating, metascore, batch_id, downloaded_at, created_at"
)


def fetch_picks(
    conn: sqlite3.Connection, username: str, *, latest_only: bool = False
) -> list[dict[str, object]]:
    """*username*'s picks, newest batch first, in the columns of the shared ones."""
    latest = (
        " AND batch_id = (SELECT MAX(batch_id) FROM user_picks WHERE username = :username)"
        if latest_only
        else ""
    )
    # rationale: only the fixed fragments above are interpolated; the username is bound
    rows = conn.execute(  # nosec B608
        f"SELECT {_COLS} FROM user_picks WHERE username = :username{latest}"
        " ORDER BY batch_id DESC, id",
        {"username": username},
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_pick(conn: sqlite3.Connection, username: str, pick_id: int) -> SuggestionDetail | None:
    row = conn.execute(
        "SELECT id, title, media_type, tmdb_id, year, description, reason, poster_url, "
        "rating, rt_rating, batch_id, downloaded_at, created_at, because_of "
        "FROM user_picks WHERE id = ? AND username = ?",
        (pick_id, username),
    ).fetchone()
    if row is None:
        return None
    fields = dict(row)
    fields["because_of"] = tuple(json.loads(row["because_of"] or "[]"))
    return SuggestionDetail(**fields)


def mark_downloaded(
    conn: sqlite3.Connection, username: str, pick_id: int, downloaded_at: str
) -> None:
    conn.execute(
        "UPDATE user_picks SET downloaded_at = ? WHERE id = ? AND username = ?",
        (downloaded_at, pick_id, username),
    )


def clear_downloaded(conn: sqlite3.Connection, username: str, ids: Sequence[object]) -> None:
    """Clear ``downloaded_at`` on *username*'s picks that Radarr/Sonarr no longer track."""
    if not ids:
        return
    with conn:
        conn.executemany(
            "UPDATE user_picks SET downloaded_at = NULL WHERE id = ? AND username = ?",
            [(pick_id, username) for pick_id in ids],
        )


def prune(conn: sqlite3.Connection, username: str, now: datetime) -> None:
    """Delete *username*'s batches and runs older than 90 days; the caller commits."""
    cutoff = now - timedelta(days=_KEEP_DAYS)
    conn.execute(
        "DELETE FROM user_picks WHERE username = ? AND batch_id < ?",
        (username, cutoff.strftime("%Y-%m-%d")),
    )
    conn.execute(
        "DELETE FROM user_pick_runs WHERE username = ? AND started_at < ?",
        (username, cutoff.isoformat()),
    )


# --- Runs ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefreshState:
    """Whether a person may have their picks made now. *reason* says why not: "running",
    "wait" (their own cooldown), "daily_limit" (theirs or everyone's for the day)."""

    reason: str | None
    next_available_at: str | None = None


def refresh_state(conn: sqlite3.Connection, username: str, now: datetime) -> RefreshState:
    if _is_running(conn, username, now):
        return RefreshState("running")
    last = conn.execute(
        "SELECT MAX(started_at) FROM user_pick_runs"
        " WHERE username = ? AND requested_by = 'person' AND status = 'done'",
        (username,),
    ).fetchone()[0]
    last_at = parse_iso_strict_utc(last) if last else None
    if last_at is not None:
        free_at = last_at + timedelta(days=REFRESH_COOLDOWN_DAYS)
        if now < free_at:
            return RefreshState("wait", free_at.isoformat())
    day_ago = (now - timedelta(days=1)).isoformat()
    for everyone, limit in ((True, REFRESHES_PER_DAY), (False, TRIES_PER_DAY)):
        # The run that must leave the window before there is room again.
        blocking = conn.execute(
            "SELECT started_at FROM user_pick_runs WHERE requested_by = 'person'"
            " AND started_at > :since AND (:everyone OR username = :username)"
            " ORDER BY started_at DESC LIMIT 1 OFFSET :limit - 1",
            {"since": day_ago, "everyone": everyone, "username": username, "limit": limit},
        ).fetchone()
        started = parse_iso_strict_utc(blocking["started_at"]) if blocking else None
        if started is not None:
            return RefreshState("daily_limit", (started + timedelta(days=1)).isoformat())
    return RefreshState(None)


def _is_running(conn: sqlite3.Connection, username: str, now: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM user_pick_runs WHERE username = ? AND finished_at IS NULL"
        " AND started_at > ?",
        (username, (now - _RUN_STALE).isoformat()),
    ).fetchone()
    return row is not None


def start_run(
    conn: sqlite3.Connection, username: str, requested_by: str, now: datetime
) -> int | RefreshState:
    """Record a run by "scan" or "person" and return its id, or why there can be none now.

    Checked and recorded under the write lock, so two requests at once start one run.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if requested_by == "person":
            state = refresh_state(conn, username, now)
            if state.reason is not None:
                return state
        elif _is_running(conn, username, now):
            return RefreshState("running")
        cursor = conn.execute(
            "INSERT INTO user_pick_runs (username, requested_by, started_at) VALUES (?, ?, ?)",
            (username, requested_by, now.isoformat()),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, now: datetime) -> None:
    with conn:
        conn.execute(
            "UPDATE user_pick_runs SET status = ?, finished_at = ? WHERE id = ?",
            (status, now.isoformat(), run_id),
        )


def last_status(conn: sqlite3.Connection, username: str) -> str | None:
    """How *username*'s last refresh they started themselves ended: done or failed."""
    row = conn.execute(
        "SELECT status FROM user_pick_runs WHERE username = ? AND requested_by = 'person'"
        " AND finished_at IS NOT NULL ORDER BY started_at DESC, id DESC LIMIT 1",
        (username,),
    ).fetchone()
    return row["status"] if row else None


def last_started(conn: sqlite3.Connection, username: str, *, done_only: bool) -> str:
    """When *username*'s last run began; with *done_only* the last that made picks. Empty
    when there was none."""
    row = conn.execute(
        "SELECT MAX(started_at) FROM user_pick_runs WHERE username = ?"
        " AND (status = 'done' OR NOT ?)",
        (username, done_only),
    ).fetchone()
    return row[0] or ""
