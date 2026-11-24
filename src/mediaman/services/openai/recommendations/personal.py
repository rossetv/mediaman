"""Make each person's own picks, from what that person played and nothing else."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import requests
from plexapi.exceptions import PlexApiException

from mediaman.core.time import now_utc
from mediaman.services.openai.client import get_openai_key
from mediaman.services.openai.recommendations import user_picks
from mediaman.services.openai.recommendations.enrich import enrich_recommendations
from mediaman.services.openai.recommendations.persist import (
    BATCH_SIZE,
    held_titles,
    new_titles,
    recent_rows,
    row_titles,
    store_batch,
)
from mediaman.services.openai.recommendations.plays import person_plays
from mediaman.services.openai.recommendations.prompts import generate_personal
from mediaman.services.openai.recommendations.repository import SHARED_ONLY

if TYPE_CHECKING:
    from mediaman.services.media_meta.plex import PlexClient, PlexRatedItem

logger = logging.getLogger(__name__)

#: Titles a person must have played before picks are made from them.
MIN_TITLES = 5
_PEOPLE_PER_SCAN = 10
_SCAN_SECONDS = 300.0
_PRESENT_WITHIN = timedelta(days=30)
_PICKS_LAST = timedelta(days=28)
# The Plex server's own account; the server token's stars are its owner's.
_OWNER_ACCOUNT_ID = 1


def has_enough_plays(conn: sqlite3.Connection, username: str) -> bool:
    return len(person_plays(conn, username)) >= MIN_TITLES


def eligible_people(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Who a scan makes picks for, the longest without a try first: people here in the last
    30 days with enough plays, something new among them or picks over 28 days old."""
    since = (now - _PRESENT_WITHIN).isoformat()
    # Playing here takes a session, which signing out deletes.
    present = conn.execute(
        "SELECT username FROM admin_users u WHERE EXISTS (SELECT 1 FROM admin_sessions s"
        "  WHERE s.username = u.username AND COALESCE(s.last_used_at, s.created_at) >= ?)"
        " OR EXISTS (SELECT 1 FROM watch_progress w"
        "  WHERE w.username = u.username AND w.updated_at >= ?)",
        (since, since),
    ).fetchall()
    due: list[tuple[str, str]] = []
    for row in present:
        name = row["username"]
        plays = person_plays(conn, name)
        if len(plays) < MIN_TITLES:
            continue
        made = user_picks.last_started(conn, name, done_only=True)
        linked = conn.execute(
            "SELECT linked_at FROM plex_links WHERE username = ?", (name,)
        ).fetchone()
        newest = max(plays[0].last_played, linked["linked_at"] if linked else "")
        if newest > made or made < (now - _PICKS_LAST).isoformat():
            due.append((user_picks.last_started(conn, name, done_only=False), name))
    return [name for _, name in sorted(due)]


def refresh_people(
    conn: sqlite3.Connection,
    plex_client: PlexClient | None,
    *,
    secret_key: str,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Make the picks of up to ten people one after another, none begun after five minutes;
    returns for how many. One person's failure leaves their old picks and the others alone."""
    try:
        get_openai_key(conn, secret_key)
    except ValueError:
        return 0
    started = clock()
    made = 0
    for username in eligible_people(conn, now_utc())[:_PEOPLE_PER_SCAN]:
        if clock() - started >= _SCAN_SECONDS:
            break
        try:
            run = user_picks.start_run(conn, username, "scan", now_utc())
            if isinstance(run, int):
                made += make_picks(conn, username, plex_client, run, secret_key=secret_key) > 0
        except (
            Exception
        ):  # rationale: one person's bad run must not cost the others theirs, nor the scan
            logger.exception("Making a person's picks failed")
    return made


def make_picks(
    conn: sqlite3.Connection,
    username: str,
    plex_client: PlexClient | None,
    run_id: int,
    *,
    secret_key: str,
) -> int:
    """Make *username*'s picks as run *run_id*, ending it; returns how many. With none, their
    earlier batch stays."""
    count = 0
    try:
        count = _make(conn, username, plex_client, secret_key)
    finally:
        user_picks.finish_run(conn, run_id, "done" if count else "failed", now_utc())
    return count


def _make(
    conn: sqlite3.Connection, username: str, plex_client: PlexClient | None, secret_key: str
) -> int:
    now = now_utc()
    plays = person_plays(conn, username)
    if len(plays) < MIN_TITLES:
        return 0
    ratings = _own_ratings(conn, username, plex_client)
    own = recent_rows(conn, now, username=username)
    shared = conn.execute(  # nosec B608 - SHARED_ONLY is a fixed string
        f"SELECT title, year, media_type, tmdb_id FROM suggestions WHERE {SHARED_ONLY} AND"
        f" batch_id = (SELECT MAX(batch_id) FROM suggestions WHERE {SHARED_ONLY})"
    ).fetchall()
    items = generate_personal(
        conn,
        [{"title": p.title, "type": p.kind} for p in plays],
        ratings,
        list(dict.fromkeys(r["title"] for r in own)),
        secret_key=secret_key,
    )
    known = [
        *((p.kind, p.tmdb_id, p.title, None) for p in plays),
        *((r["type"], None, r["title"], None) for r in ratings),
        *held_titles(conn),
        *row_titles(own),
        *row_titles(shared),
    ]
    enrich_recommendations(items, conn, secret_key, accept=new_titles(known), limit=BATCH_SIZE)
    if not items:
        return 0
    uses_plex = bool(ratings) or any(p.in_plex for p in plays)
    return store_batch(conn, items, now, username=username, uses_plex=uses_plex, prune=True)


def _own_ratings(
    conn: sqlite3.Connection, username: str, plex_client: PlexClient | None
) -> list[PlexRatedItem]:
    """The stars *username* gave: the server token's, so only for the server's owner."""
    if plex_client is None:
        return []
    owner = conn.execute(
        "SELECT 1 FROM plex_links WHERE username = ? AND server_account_id = ?",
        (username, _OWNER_ACCOUNT_ID),
    ).fetchone()
    if owner is None:
        return []
    try:
        return plex_client.get_user_ratings()
    except (PlexApiException, requests.RequestException):
        logger.warning("Failed to fetch Plex user ratings — proceeding without them", exc_info=True)
        return []
