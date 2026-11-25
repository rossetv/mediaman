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
from mediaman.services.openai.recommendations.enrich import (
    ENRICH_SECONDS,
    enrich_recommendations,
)
from mediaman.services.openai.recommendations.mix import choose_mix
from mediaman.services.openai.recommendations.persist import (
    BATCH_SIZE,
    held_titles,
    new_titles,
    recent_rows,
    row_titles,
    store_batch,
)
from mediaman.services.openai.recommendations.plays import PlayedTitle, person_plays
from mediaman.services.openai.recommendations.prompts import (
    Portrait,
    generate_personal,
    make_portrait,
)
from mediaman.services.openai.recommendations.repository import SHARED_ONLY
from mediaman.services.openai.recommendations.taste import Taste, read_taste
from mediaman.services.plex_link.plex_tv import OWNER_ACCOUNT_ID
from mediaman.services.plex_link.repository import PlexLink, get_link

if TYPE_CHECKING:
    from mediaman.services.media_meta.plex import PlexClient, PlexRatedItem

logger = logging.getLogger(__name__)

#: Titles a person must have played before picks are made from them.
MIN_TITLES = 5
_PEOPLE_PER_SCAN = 10
_SCAN_SECONDS = 300.0
# One person's picks: the model's answer and the look-ups after it, which get what is left.
_PERSON_SECONDS = 180.0
# The second call to the model begins only with this much of it left.
_MIX_SECONDS = 30.0
_PRESENT_WITHIN = timedelta(days=30)
_FAILED_WAITS = timedelta(hours=24)


def has_enough_plays(conn: sqlite3.Connection, username: str) -> bool:
    return len(person_plays(conn, username)) >= MIN_TITLES


def eligible_people(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Who a scan makes picks for, the longest without a try first: people here in the last
    30 days with enough plays, something new among them, no picks, or picks over 28 days old. One whose last run
    failed waits a day."""
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
        if user_picks.failed_since(conn, name, now - _FAILED_WAITS):
            continue
        plays = person_plays(conn, name)
        if len(plays) < MIN_TITLES:
            continue
        made = user_picks.last_started(conn, name, done_only=True)
        link = get_link(conn, name)
        newest = max(plays[0].last_played, link.linked_at if link else "")
        if (
            newest > made
            or made < (now - user_picks.PICKS_LAST).isoformat()
            or not user_picks.fetch_picks(conn, name, latest_only=True)
        ):
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
        except Exception:  # rationale: one bad run must not cost the others theirs
            logger.exception("Making a person's picks failed")
    return made


def make_picks(
    conn: sqlite3.Connection,
    username: str,
    plex_client: PlexClient | None,
    run_id: int,
    *,
    secret_key: str,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Make *username*'s picks as run *run_id*, ending it; returns how many. With none, their
    earlier batch stays."""
    count = 0
    try:
        count = _make(conn, username, plex_client, run_id, secret_key, clock)
    finally:
        user_picks.finish_run(conn, run_id, "done" if count else "failed", now_utc())
    return count


def _make(
    conn: sqlite3.Connection,
    username: str,
    plex_client: PlexClient | None,
    run_id: int,
    secret_key: str,
    clock: Callable[[], float],
) -> int:
    ends_at = clock() + _PERSON_SECONDS
    now = now_utc()
    # Before the plays, so plays of a link made after it cannot pass for its.
    link = get_link(conn, username)
    plays = person_plays(conn, username)
    if len(plays) < MIN_TITLES:
        return 0
    ratings = _own_ratings(link, plex_client)
    own = recent_rows(conn, now, username=username)
    shared = conn.execute(  # nosec B608 - SHARED_ONLY is a fixed string
        f"SELECT title, year, media_type, tmdb_id FROM suggestions WHERE {SHARED_ONLY} AND"
        f" batch_id = (SELECT MAX(batch_id) FROM suggestions WHERE {SHARED_ONLY})"
    ).fetchall()
    portrait = _portrait(plays, ratings, read_taste(conn, username), [r["title"] for r in own])
    items = generate_personal(conn, portrait, secret_key=secret_key)
    # Read again: they may have rated something while the model was thinking.
    taste = read_taste(conn, username)
    known = [
        *((p.kind, p.tmdb_id, p.title, None) for p in plays),
        *((r["type"], None, r["title"], None) for r in ratings),
        *held_titles(conn),
        *row_titles(own),
        *row_titles(shared),
        # One rated up is known to them; one rated "not for me" may return, last.
        *((r.media_type, r.tmdb_id, r.title or "", r.year) for r in taste.ratings if r.level != -1),
    ]
    enrich_recommendations(
        items,
        conn,
        secret_key,
        accept=new_titles(known),
        choose=lambda found: choose_mix(
            found,
            BATCH_SIZE,
            conn,
            portrait=portrait,
            levels=taste.levels,
            may_ask=ends_at - clock() >= _MIX_SECONDS,
            secret_key=secret_key,
        ),
        seconds=min(ENRICH_SECONDS, ends_at - clock()),
        clock=clock,
    )
    if not items:
        return 0
    uses_plex = bool(ratings) or any(p.in_plex for p in plays)
    return store_batch(
        conn,
        items,
        now,
        username=username,
        run_id=run_id,
        uses_plex=uses_plex,
        linked_at=link.linked_at if link else None,
    )


def _portrait(
    plays: list[PlayedTitle], stars: list[PlexRatedItem], taste: Taste, previous: list[str]
) -> Portrait:
    """What the model is told. Someone who keeps their history from the AI has the names of
    what they played, their stars and earlier picks sent, and nothing more."""
    levels = taste.levels
    history: list[dict[str, object]] = [
        {
            "title": p.title,
            "type": p.kind,
            # Not sent: it keeps the title from being what led to a pick.
            "disliked": p.tmdb_id is not None and levels.get((p.kind, p.tmdb_id), 0) < 0,
        }
        for p in plays
    ]
    if not taste.may_send:
        return make_portrait(history, stars, list(dict.fromkeys(previous)))
    ratings: list[dict[str, object]] = [
        {"title": r.title, "type": r.media_type, "level": r.level} for r in taste.ratings if r.title
    ]
    rated = {(r["type"], r["title"]) for r in ratings}
    return make_portrait(
        [{**h, "note": p.note} for h, p in zip(history, plays, strict=True)],
        # What they said here is newer than a star in Plex.
        [*ratings, *(dict(s) for s in stars if (s["type"], s["title"]) not in rated)],
        list(dict.fromkeys(previous)),
        asked_for=taste.asked_for,
        searches=taste.searches,
    )


def _own_ratings(link: PlexLink | None, plex_client: PlexClient | None) -> list[PlexRatedItem]:
    """The stars of the person of *link*: the server token's, so only for the server's owner."""
    if plex_client is None or link is None or link.server_account_id != OWNER_ACCOUNT_ID:
        return []
    try:
        return plex_client.get_user_ratings()
    except (PlexApiException, requests.RequestException):
        logger.warning("Failed to fetch Plex user ratings — proceeding without them", exc_info=True)
        return []
