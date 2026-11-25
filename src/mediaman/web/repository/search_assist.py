"""Repository functions for the AI in search: a person's recent searches, the phrases and
answers made for them, and the runs that count against the limits.

Every read and write names the one person it is for, save the counts of everyone's runs.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mediaman.services.openai.search_assist import Taste

#: Questions a person may ask in a day, and runs of every kind for everyone together.
QUESTIONS_PER_DAY = 20
EVERYONE_PER_DAY = 100
#: Runs for everyone at one time.
AT_ONCE = 4
RECENT_SHOWN = 6
PHRASE_MAX = 200
PHRASES_LAST = timedelta(days=1)
ANSWERS_LAST = timedelta(days=7)
# A run left unfinished this long ago died with its process.
_RUN_STALE = timedelta(minutes=5)
# Longer than either limit looks back.
_RUNS_KEPT = timedelta(days=2)
_TASTE_LIMIT = 20

PHRASES = "phrases"

_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]+")


def user_id_of(conn: sqlite3.Connection, username: str) -> int | None:
    row = conn.execute("SELECT id FROM admin_users WHERE username = ?", (username,)).fetchone()
    return row["id"] if row else None


# --- History switch ------------------------------------------------------------


def uses_history(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT ai_history FROM admin_users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["ai_history"])


def set_uses_history(conn: sqlite3.Connection, user_id: int, enabled: bool) -> None:
    """Switched off, what was made from the history goes, and what is being made stops."""
    with conn:
        conn.execute("UPDATE admin_users SET ai_history = ? WHERE id = ?", (enabled, user_id))
        if not enabled:
            _drop_made(conn, user_id)


def _drop_made(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM user_ai_phrases WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_ai_answers WHERE user_id = ?", (user_id,))
    conn.execute(
        "UPDATE user_ai_runs SET status = 'cancelled' WHERE user_id = ? AND status = 'running'",
        (user_id,),
    )


# --- Recent searches -----------------------------------------------------------


def tidy(text: str) -> str:
    """*text* on one line, as it is stored and asked."""
    return _SPACE_RE.sub(" ", text).strip()[:PHRASE_MAX].strip()


def record_search(conn: sqlite3.Connection, user_id: int, text: str, now: datetime) -> bool:
    """Keep a search of the person; False, and nothing kept, when their history is not used."""
    phrase = tidy(text)
    if not phrase:
        return False
    with conn:
        cursor = conn.execute(
            "INSERT INTO user_searches (user_id, phrase_key, phrase, searched_at)"
            " SELECT id, :key, :phrase, :at FROM admin_users WHERE id = :id AND ai_history = 1"
            " ON CONFLICT (user_id, phrase_key) DO UPDATE SET phrase = excluded.phrase,"
            " searched_at = excluded.searched_at",
            {"id": user_id, "key": phrase.casefold(), "phrase": phrase, "at": now.isoformat()},
        )
    return cursor.rowcount > 0


def recent_searches(conn: sqlite3.Connection, user_id: int, limit: int) -> list[str]:
    rows = conn.execute(
        "SELECT phrase FROM user_searches WHERE user_id = ?"
        " ORDER BY searched_at DESC, rowid DESC LIMIT ?",
        (user_id, limit),
    )
    return [r["phrase"] for r in rows]


def clear_searches(conn: sqlite3.Connection, user_id: int) -> None:
    """Forget the person's searches, and the phrases and answers made from them."""
    with conn:
        conn.execute("DELETE FROM user_searches WHERE user_id = ?", (user_id,))
        _drop_made(conn, user_id)


def taste_of(conn: sqlite3.Connection, user_id: int, played: list[str]) -> Taste:
    """What may be said of the person to the model: nothing when their history is not used."""
    if not uses_history(conn, user_id):
        return Taste()
    rows = conn.execute(
        "SELECT title, level FROM user_ratings WHERE user_id = ? AND title IS NOT NULL"
        " ORDER BY updated_at DESC",
        (user_id,),
    ).fetchall()
    return Taste(
        searches=recent_searches(conn, user_id, _TASTE_LIMIT),
        played=played[:_TASTE_LIMIT],
        liked=[r["title"] for r in rows if r["level"] > 0][:_TASTE_LIMIT],
        disliked=[r["title"] for r in rows if r["level"] < 0][:_TASTE_LIMIT],
    )


# --- Phrases -------------------------------------------------------------------


def stored_phrases(conn: sqlite3.Connection, user_id: int) -> list[str]:
    row = conn.execute(
        "SELECT p.phrases FROM user_ai_phrases p JOIN admin_users u ON u.id = p.user_id"
        " WHERE p.user_id = ? AND u.ai_history = 1",
        (user_id,),
    ).fetchone()
    return list(json.loads(row["phrases"])) if row else []


def phrases_due(conn: sqlite3.Connection, user_id: int, now: datetime) -> bool:
    """Whether the phrases may be made now: once a day, a failed try counted."""
    row = conn.execute(
        "SELECT 1 FROM user_ai_runs WHERE user_id = ? AND kind = ? AND started_at > ?",
        (user_id, PHRASES, (now - PHRASES_LAST).isoformat()),
    ).fetchone()
    return row is None and uses_history(conn, user_id)


# --- Answers -------------------------------------------------------------------


def fetch_answer(
    conn: sqlite3.Connection, user_id: int, kind: str, key: str, now: datetime
) -> Any | None:
    row = conn.execute(
        "SELECT answer FROM user_ai_answers"
        " WHERE user_id = ? AND kind = ? AND query_key = ? AND made_at > ?",
        (user_id, kind, key, (now - ANSWERS_LAST).isoformat()),
    ).fetchone()
    return json.loads(row["answer"]) if row else None


def drop_expired(conn: sqlite3.Connection, now: datetime) -> None:
    """Delete everyone's answers that are past their time."""
    with conn:
        conn.execute(
            "DELETE FROM user_ai_answers WHERE made_at <= ?", ((now - ANSWERS_LAST).isoformat(),)
        )


# --- Runs ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Refused:
    """Why there can be no run now: "running" (*run_id* goes on), "stopping" (the call of one
    that was stopped has yet to end), "busy", "limit" (the
    person's) or "limit_everyone"."""

    reason: str
    run_id: int | None = None
    resets_at: str | None = None


def _midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def start_run(conn: sqlite3.Connection, user_id: int, kind: str, now: datetime) -> int | Refused:
    """Record a run and return its id, or why there can be none now.

    Checked and recorded under the write lock, so two requests at once start one run. The
    phrases are made unasked, so they neither hold up a question nor count as one. A stopped
    run holds its place until its call has ended.
    """
    today = _midnight(now)
    own = "kind = :phrases" if kind == PHRASES else "kind <> :phrases"
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE user_ai_runs SET finished_at = ?,"
            " status = CASE status WHEN 'running' THEN 'failed' ELSE status END"
            " WHERE finished_at IS NULL AND started_at <= ?",
            (now.isoformat(), (now - _RUN_STALE).isoformat()),
        )
        conn.execute(
            "DELETE FROM user_ai_runs WHERE finished_at IS NOT NULL AND started_at < ?",
            ((now - _RUNS_KEPT).isoformat(),),
        )
        # rationale: only the fixed fragment above is interpolated; every value is bound
        mine = conn.execute(  # nosec B608
            "SELECT MAX(CASE WHEN finished_at IS NULL THEN id END) AS open,"
            " MAX(CASE WHEN status = 'running' THEN id END) AS running,"
            " COUNT(CASE WHEN started_at >= :today THEN 1 END) AS today"
            f" FROM user_ai_runs WHERE user_id = :user AND {own}",
            {"today": today.isoformat(), "user": user_id, "phrases": PHRASES},
        ).fetchone()
        everyone = conn.execute(
            "SELECT COUNT(CASE WHEN finished_at IS NULL THEN 1 END) AS open,"
            " COUNT(CASE WHEN started_at >= ? THEN 1 END) AS today FROM user_ai_runs"
            " WHERE started_at >= ? OR finished_at IS NULL",
            (today.isoformat(), today.isoformat()),
        ).fetchone()
        if mine["running"] is not None:
            return Refused("running", run_id=mine["running"])
        if mine["open"] is not None:
            return Refused("stopping")
        resets_at = (today + timedelta(days=1)).isoformat()
        if kind != PHRASES and mine["today"] >= QUESTIONS_PER_DAY:
            return Refused("limit", resets_at=resets_at)
        if everyone["today"] >= EVERYONE_PER_DAY:
            return Refused("limit_everyone", resets_at=resets_at)
        if everyone["open"] >= AT_ONCE:
            return Refused("busy")
        cursor = conn.execute(
            "INSERT INTO user_ai_runs (user_id, kind, started_at) VALUES (?, ?, ?)",
            (user_id, kind, now.isoformat()),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def run_status(conn: sqlite3.Connection, user_id: int, run_id: int, now: datetime) -> str | None:
    """ "running", "done", "failed" or "cancelled"; None when it is no run of the person."""
    row = conn.execute(
        "SELECT status, started_at FROM user_ai_runs WHERE id = ? AND user_id = ?",
        (run_id, user_id),
    ).fetchone()
    if row is None:
        return None
    if row["status"] == "running" and row["started_at"] <= (now - _RUN_STALE).isoformat():
        return "failed"
    return str(row["status"])


def stop_run(conn: sqlite3.Connection, user_id: int, run_id: int) -> None:
    """Have the run keep nothing; it ends once its call has."""
    with conn:
        conn.execute(
            "UPDATE user_ai_runs SET status = 'cancelled'"
            " WHERE id = ? AND user_id = ? AND status = 'running'",
            (run_id, user_id),
        )


def _end(conn: sqlite3.Connection, user_id: int, run_id: int, status: str, now: datetime) -> bool:
    cursor = conn.execute(
        "UPDATE user_ai_runs SET status = ?, finished_at = ?"
        " WHERE id = ? AND user_id = ? AND status = 'running'",
        (status, now.isoformat(), run_id, user_id),
    )
    return cursor.rowcount > 0


def call_ended(conn: sqlite3.Connection, run_id: int, now: datetime) -> None:
    """Free the place of the run, which failed if nothing has said otherwise."""
    with conn:
        conn.execute(
            "UPDATE user_ai_runs SET finished_at = ?,"
            " status = CASE status WHEN 'running' THEN 'failed' ELSE status END"
            " WHERE id = ? AND finished_at IS NULL",
            (now.isoformat(), run_id),
        )


def runs_there(conn: sqlite3.Connection) -> set[int]:
    return {r["id"] for r in conn.execute("SELECT id FROM user_ai_runs")}


def finish_with_phrases(
    conn: sqlite3.Connection, user_id: int, run_id: int, phrases: list[str], now: datetime
) -> None:
    """Keep what the run made, unless it was cancelled meanwhile (cleared or switched off)."""
    with conn:
        if _end(conn, user_id, run_id, "done", now):
            conn.execute(
                "INSERT OR REPLACE INTO user_ai_phrases (user_id, phrases, made_at)"
                " SELECT id, ?, ? FROM admin_users WHERE id = ? AND ai_history = 1",
                (json.dumps(phrases), now.isoformat(), user_id),
            )


def finish_with_answer(
    conn: sqlite3.Connection,
    user_id: int,
    run_id: int,
    kind: str,
    key: str,
    answer: Any,
    now: datetime,
) -> bool:
    """As ``finish_with_phrases``, for an answer. Of someone whose history is not used it is
    not stored. Whether the run was not cancelled."""
    with conn:
        kept = _end(conn, user_id, run_id, "done", now)
        if kept:
            conn.execute(
                "INSERT OR REPLACE INTO user_ai_answers (user_id, kind, query_key, answer, made_at)"
                " SELECT id, ?, ?, ?, ? FROM admin_users WHERE id = ? AND ai_history = 1",
                (kind, key, json.dumps(answer), now.isoformat(), user_id),
            )
    return kept


# --- Tonight -------------------------------------------------------------------


def begun_refs(conn: sqlite3.Connection, username: str) -> set[str]:
    """The refs of the library titles *username* began, finished or marked, anything of a
    show counting as the show."""
    rows = conn.execute(
        "SELECT CASE WHEN show_rating_key IS NULL THEN 'movie-' || COALESCE(media_item_id,"
        " rating_key) ELSE 'show-' || show_rating_key END AS ref FROM watch_progress"
        " WHERE username = :name AND (position_s > 0 OR watched = 1 OR seen_at IS NOT NULL)"
        " UNION SELECT CASE WHEN media_type = 'movie' THEN 'movie-' || rating_key"
        " ELSE 'show-' || show_rating_key END FROM plex_plays WHERE username = :name",
        {"name": username},
    )
    return {r["ref"] for r in rows if r["ref"]}


def first_seasons(conn: sqlite3.Connection) -> dict[str, int]:
    """How many episodes the library holds of each show's first season, where known."""
    rows = conn.execute(
        "SELECT show_rating_key, MAX(episode_count) AS episodes FROM media_items"
        " WHERE season_number = 1 AND show_rating_key IS NOT NULL AND episode_count > 0"
        " GROUP BY show_rating_key"
    )
    return {r["show_rating_key"]: r["episodes"] for r in rows}
