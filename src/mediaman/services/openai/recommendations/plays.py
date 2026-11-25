"""What one person played: the plays their own picks are made from.

Every read takes one username and reads only that username's rows.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.services.watch_catalog import normalise_title

_PLAYS_SQL = """
SELECT wp.show_rating_key IS NULL AS is_movie,
       CASE WHEN wp.show_rating_key IS NULL THEN mi.title
            ELSE COALESCE(mi.show_title, s.show_title, mi.title) END AS title,
       CASE WHEN wp.show_rating_key IS NULL THEN mi.tmdb_id
            ELSE COALESCE(s.tmdb_id, mi.tmdb_id) END AS tmdb_id,
       wp.updated_at AS played_at, 0 AS in_plex, wp.rating_key AS part,
       (wp.watched = 1 OR wp.seen_at IS NOT NULL) AS done,
       CASE WHEN wp.watched = 1 AND julianday(wp.updated_at) - julianday(wp.seen_at) >= 7
            THEN 1 ELSE 0 END AS again,
       CASE WHEN wp.duration_s > 0 THEN wp.position_s / wp.duration_s END AS share
FROM watch_progress wp
LEFT JOIN media_items mi ON mi.id = wp.media_item_id
LEFT JOIN (SELECT show_rating_key, MAX(show_title) AS show_title, MAX(tmdb_id) AS tmdb_id
           FROM media_items WHERE show_rating_key IS NOT NULL GROUP BY show_rating_key) s
  ON s.show_rating_key = wp.show_rating_key
-- A row made by a mark was never played; its time is the epoch's.
WHERE wp.username = :username AND wp.updated_at > '1971'
  AND (wp.credited_at IS NOT NULL OR wp.watched = 1 OR wp.seen_at IS NOT NULL)
UNION ALL
SELECT p.media_type = 'movie', CASE WHEN p.media_type = 'movie' THEN p.title ELSE p.show_title END,
       CASE WHEN p.media_type = 'movie' THEN
         (SELECT tmdb_id FROM media_items WHERE id = p.rating_key AND media_type = 'movie')
       ELSE (SELECT MAX(tmdb_id) FROM media_items WHERE show_rating_key = p.show_rating_key) END,
       p.viewed_at, 1, p.rating_key, 1, 0, NULL
FROM plex_plays p
WHERE p.username = :username AND p.media_type IN ('movie', 'episode')
UNION ALL
SELECT media_type = 'movie', title, tmdb_id, played_at, 0, NULL, 0, 0, NULL
FROM played_titles WHERE username = :username
ORDER BY 4 DESC
"""


_RECORD_SQL = """
INSERT INTO played_titles (username, media_type, title, tmdb_id, played_at)
SELECT :username, CASE WHEN :show IS NULL THEN 'movie' ELSE 'tv' END,
       CASE WHEN :show IS NULL THEN title ELSE COALESCE(show_title, title) END, tmdb_id, :at
FROM media_items WHERE id = :item OR show_rating_key = :show
ORDER BY id IS :item DESC LIMIT 1
ON CONFLICT (username, media_type, title) DO UPDATE SET
    played_at = MAX(played_at, excluded.played_at), tmdb_id = COALESCE(excluded.tmdb_id, tmdb_id)
"""


def record_play(
    conn: sqlite3.Connection,
    username: str,
    media_item_id: str | None,
    show_rating_key: str | None,
    played_at: str,
) -> None:
    """Keep the title of *username*'s play, which a deletion of the title would lose."""
    conn.execute(
        _RECORD_SQL,
        {"username": username, "item": media_item_id, "show": show_rating_key, "at": played_at},
    )


@dataclass(frozen=True, slots=True)
class PlayedTitle:
    kind: Literal["movie", "tv"]
    title: str
    tmdb_id: int | None
    last_played: str
    #: Known from a play in one of Plex's own apps, not from mediaman alone.
    in_plex: bool
    #: How it went, as far as is known: "finished", "watched again", "left part way", "3 episodes".
    note: str = field(default="", compare=False)


# How long a title must lie untouched to count as left.
_LEFT_AFTER = timedelta(days=45)
_AGAIN_AFTER = timedelta(days=7)


@dataclass(slots=True)
class _Course:
    """What the rows of one title tell of how it went."""

    parts: set[str] = field(default_factory=set)
    last_done: dict[str, datetime] = field(default_factory=dict)
    again: bool = False
    share: float = 0.0

    def add(self, row: sqlite3.Row) -> None:
        self.share = max(self.share, row["share"] or 0.0)
        if not row["done"]:
            return
        part = row["part"] or ""
        self.parts.add(part)
        at = parse_iso_strict_utc(row["played_at"])
        before = self.last_done.get(part)
        if at is not None:
            # A film of two copies, or on Plex and here, is one part under two keys: only plays
            # of one key are counted again.
            if before is not None and abs(before - at) >= _AGAIN_AFTER:
                self.again = True
            self.last_done[part] = at
        self.again = self.again or bool(row["again"])

    def note(self, kind: str, idle: timedelta | None) -> str:
        left = idle is not None and idle >= _LEFT_AFTER
        if self.again:
            return "watched again"
        if kind == "movie":
            if self.parts:
                return "finished"
            return "left part way" if left and 0.05 < self.share < 0.85 else ""
        count = len(self.parts)
        episodes = f"{count} episode{'' if count == 1 else 's'}" if count else ""
        return f"left after {episodes}" if left and 0 < count <= 2 else episodes


def person_plays(conn: sqlite3.Connection, username: str) -> list[PlayedTitle]:
    """The movies and shows *username* played, each once, the last played first.

    They are what was played on mediaman and, for someone who linked their Plex account,
    what ``plex_plays`` holds for them.
    """
    found: dict[tuple[str, str], PlayedTitle] = {}
    keys: dict[tuple[str, int], tuple[str, str]] = {}
    courses: defaultdict[tuple[str, str], _Course] = defaultdict(_Course)
    for row in conn.execute(_PLAYS_SQL, {"username": username}):
        title = (row["title"] or "").strip()
        kind: Literal["movie", "tv"] = "movie" if row["is_movie"] else "tv"
        key: tuple[str, str] = (kind, normalise_title(title))
        if not key[1]:
            continue
        if row["tmdb_id"]:
            # A title renamed since it was played is one title.
            key = keys.setdefault((kind, row["tmdb_id"]), key)
        courses[key].add(row)
        held = found.get(key)
        found[key] = PlayedTitle(
            kind=kind,
            title=held.title if held else title,
            tmdb_id=(held.tmdb_id if held else None) or row["tmdb_id"],
            last_played=held.last_played if held else row["played_at"],
            in_plex=bool(row["in_plex"]) or (held is not None and held.in_plex),
        )
    now = now_utc()
    plays = []
    for key, play in found.items():
        at = parse_iso_strict_utc(play.last_played)
        plays.append(replace(play, note=courses[key].note(play.kind, now - at if at else None)))
    return plays
