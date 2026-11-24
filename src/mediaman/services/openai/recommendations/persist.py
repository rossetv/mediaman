"""Make and save the shared recommendations, and what saving a person's own shares with it."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from mediaman.core.time import now_utc
from mediaman.services.downloads.download_format._parsing import normalise_for_match
from mediaman.services.openai.recommendations._types import RecommendationItem
from mediaman.services.openai.recommendations.enrich import enrich_recommendations
from mediaman.services.openai.recommendations.prompts import (
    generate_trending,
    strip_season_suffix,
)
from mediaman.services.openai.recommendations.repository import SHARED_ONLY
from mediaman.services.plex_link.repository import get_link
from mediaman.services.watch_catalog import SeenTitles

logger = logging.getLogger(__name__)

#: Picks kept of a batch, of the larger number asked for.
BATCH_SIZE = 14


def recent_rows(
    conn: sqlite3.Connection, now: datetime, *, username: str | None = None
) -> list[sqlite3.Row]:
    """The shared picks of the last 30 days, or *username*'s own, newest first.

    Those of today already downloaded are left out: a second run today may pick them again,
    which keeps them and their mark.
    """
    table, owned = _table_of(username)
    today = now.strftime("%Y-%m-%d")
    cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    # batch_id is always a fixed-width "YYYY-MM-DD" string, so lexical >= equals
    # chronological >= here.
    # rationale: only the fixed fragments of _table_of are interpolated; every value is bound
    return conn.execute(  # nosec B608
        f"SELECT title, year, media_type, tmdb_id FROM {table} WHERE {owned}"
        " AND batch_id >= :cutoff AND NOT (batch_id = :today AND downloaded_at IS NOT NULL)"
        " ORDER BY batch_id DESC, id",
        {"username": username, "cutoff": cutoff_30d, "today": today},
    ).fetchall()


def _table_of(username: str | None) -> tuple[str, str]:
    """The table of *username*'s picks, or of the shared ones, and the rows in it that count."""
    if username is None:
        return "suggestions", SHARED_ONLY
    return "user_picks", "username = :username"


Title = tuple[str, int | None, str, int | None]

_TRAILING_YEAR_RE = re.compile(r"\s*\((\d{4})\)\s*$")


def held_titles(conn: sqlite3.Connection) -> list[Title]:
    """(media type, TMDB id, title, year) of what the library holds or mediaman deleted."""
    rows = conn.execute(
        "SELECT media_type, tmdb_id, COALESCE(show_title, title) AS title FROM media_items"
        " UNION SELECT media_type, tmdb_id, title FROM deleted_media"
    )
    found: list[Title] = []
    for row in rows:
        title = row["title"] or ""
        year = _TRAILING_YEAR_RE.search(title)
        found.append(
            (
                "movie" if row["media_type"] == "movie" else "tv",
                row["tmdb_id"],
                title[: year.start()] if year else title,
                int(year.group(1)) if year else None,
            )
        )
    return found


def row_titles(rows: Iterable[sqlite3.Row]) -> list[Title]:
    return [(r["media_type"], r["tmdb_id"], r["title"], r["year"]) for r in rows]


def new_titles(known: Iterable[Title]) -> Callable[[RecommendationItem], bool]:
    """A test that a pick is none of *known*, nor one it was asked about before."""
    seen = SeenTitles()
    # Those with an id first: one without would otherwise keep the id of its namesake out.
    for kind, tmdb_id, title, year in sorted(known, key=lambda t: t[1] is None):
        seen.add(kind, tmdb_id, _whole_title(kind, title), year)

    def is_new(item: RecommendationItem) -> bool:
        kind = item["media_type"]
        title = _whole_title(kind, item["title"])
        return seen.add(kind, item.get("tmdb_id"), title, item.get("year"))

    return is_new


def _whole_title(kind: str, title: str) -> str:
    return (strip_season_suffix(title) or title) if kind == "tv" else title


def _match_downloaded(
    downloaded: list[sqlite3.Row], item: RecommendationItem, taken: set[int]
) -> sqlite3.Row | None:
    """Return the not-yet-taken downloaded row that *item* repeats, if any.

    The media type and the normalised full title (season suffix included) must match;
    tmdb_id and year must also match when both sides have one.
    """
    tmdb_id = item.get("tmdb_id")
    year = item.get("year")
    title = normalise_for_match(str(item.get("title") or ""))
    for row in downloaded:
        if row["id"] in taken or row["media_type"] != item["media_type"]:
            continue
        if normalise_for_match(row["title"]) != title:
            continue
        if tmdb_id and row["tmdb_id"] and tmdb_id != row["tmdb_id"]:
            continue
        if year and row["year"] and year != row["year"]:
            continue
        return row
    return None


_STORED = (
    "title, media_type, year, tmdb_id, imdb_id, description, reason, poster_url, trailer_url, "
    "rating, rt_rating, tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, "
    "metascore, backdrop_url"
)


def store_batch(
    conn: sqlite3.Connection,
    items: list[RecommendationItem],
    now: datetime,
    *,
    username: str | None = None,
    uses_plex: bool = False,
    linked_at: str | None = None,
) -> int:
    """Replace today's batch, *username*'s own or the shared one, with *items* in one
    transaction; returns how many were stored. A pick of today already downloaded keeps its
    mark. Rolls back on any exception, so the caller must not wrap it in another.

    Picks made from plays in Plex's apps (*uses_plex*) are stored only while the person's Plex
    link is the one made at *linked_at*, when the plays were read: not once they unlinked, nor
    when they linked another account since.
    """
    table, owned = _table_of(username)
    today = now.strftime("%Y-%m-%d")
    inserted = 0
    with conn:  # rolls back DELETE + any partial INSERTs on exception
        # Take the write lock before reading the marks so a download click cannot
        # commit between that read and the DELETE and be lost.
        conn.execute("BEGIN IMMEDIATE")
        if uses_plex and username is not None:
            link = get_link(conn, username)
            if link is None or link.linked_at != linked_at:
                return 0
        # rationale: only the fixed fragments of _table_of are interpolated; every value is bound
        downloaded = conn.execute(  # nosec B608
            f"SELECT id, title, year, media_type, tmdb_id, downloaded_at FROM {table} "
            f"WHERE {owned} AND batch_id = :today AND downloaded_at IS NOT NULL",
            {"username": username, "today": today},
        ).fetchall()
        marks: list[str | None] = []
        matched_ids: set[int] = set()
        for s in items:
            row = _match_downloaded(downloaded, s, matched_ids)
            marks.append(row["downloaded_at"] if row else None)
            if row:
                matched_ids.add(row["id"])
        # A downloaded suggestion absent from the new set stays in today's list.
        kept_ids = {r["id"] for r in downloaded} - matched_ids
        gone = conn.execute(  # nosec B608
            f"SELECT id FROM {table} WHERE {owned} AND batch_id = :today",
            {"username": username, "today": today},
        ).fetchall()
        conn.executemany(
            f"DELETE FROM {table} WHERE id = ?",  # nosec B608
            [(r["id"],) for r in gone if r["id"] not in kept_ids],
        )

        for s, downloaded_at in zip(items, marks, strict=True):
            values: dict[str, object] = {c: s.get(c) for c in _STORED.split(", ")}
            values |= {
                "title": str(s.get("title") or ""),
                "media_type": s["media_type"],
                "reason": str(s.get("reason") or ""),
                "batch_id": today,
                "created_at": now.isoformat(),
                "downloaded_at": downloaded_at,
            }
            if username is None:
                values["category"] = s["category"]
            else:
                values["username"] = username
                values["because_of"] = json.dumps(s.get("because_of") or [], ensure_ascii=False)
                values["uses_plex"] = int(uses_plex)
            # A downloaded pick kept above can be the same title under another spelling.
            # rationale: the column names are the fixed ones above; every value is bound
            cursor = conn.execute(  # nosec B608
                f"INSERT INTO {table} ({', '.join(values)}) "
                f"VALUES ({', '.join('?' * len(values))}) ON CONFLICT DO NOTHING",
                tuple(values.values()),
            )
            inserted += cursor.rowcount
    return inserted


def prune_old_picks(conn: sqlite3.Connection) -> None:
    """Delete everyone's picks and runs, and the shared picks, older than 90 days."""
    cutoff = now_utc() - timedelta(days=90)
    with conn:
        for table in ("suggestions", "user_picks"):
            # rationale: the table names are the two above; the value is bound
            conn.execute(  # nosec B608
                f"DELETE FROM {table} WHERE batch_id < ?", (cutoff.strftime("%Y-%m-%d"),)
            )
        conn.execute("DELETE FROM user_pick_runs WHERE started_at < ?", (cutoff.isoformat(),))


def refresh_recommendations(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
) -> int:
    """Generate the new and popular recommendations everyone sees; returns the count
    generated. No viewing data goes into them. Every run replaces today's batch.
    """
    now = now_utc()
    earlier = recent_rows(conn, now)
    trending = generate_trending(
        conn, list(dict.fromkeys(r["title"] for r in earlier)), secret_key=secret_key
    )
    if not trending:
        return 0

    enrich_recommendations(
        trending,
        conn,
        secret_key,
        accept=new_titles([*held_titles(conn), *row_titles(earlier)]),
        limit=BATCH_SIZE,
    )
    if not trending:
        return 0

    inserted = store_batch(conn, trending, now)
    logger.info("Generated %d shared recommendations; inserted=%d", len(trending), inserted)
    return inserted
