"""Repository functions for how each person rated films and shows.

Every read is scoped to one person: a rating is never shown to anyone else, and
nothing that deletes or keeps titles reads it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from mediaman.services.watch_catalog import folder_year

STRONG_NO = -2


@dataclass(frozen=True, slots=True)
class RatedTitle:
    """A title that can be rated. *media_type* is "movie" or "tv"."""

    media_type: str
    tmdb_id: int
    title: str | None = None
    year: int | None = None


@dataclass(frozen=True, slots=True)
class SeasonEnd:
    """The season an episode is of, and whether the library holds none after it."""

    show_key: str
    number: int
    episode: int
    last: bool


def levels_for(conn: sqlite3.Connection, user_id: int) -> dict[tuple[str, int], int]:
    """The person's level (-2, -1, 1 or 2) of each title they rated, by media type and TMDB id."""
    rows = conn.execute(
        "SELECT media_type, tmdb_id, level FROM user_ratings WHERE user_id = ?", (user_id,)
    )
    return {(r["media_type"], r["tmdb_id"]): r["level"] for r in rows}


def suggestible(
    rows: Iterable[dict[str, object]],
    levels: Mapping[tuple[str, int], int],
    seen: Container[tuple[str, int]] = (),
) -> list[dict[str, object]]:
    """The suggestions (with ``media_type`` and ``tmdb_id``) to show a person with *levels* who
    marked *seen* watched: none of these or of their strong noes, and those they rated down
    after the rest."""
    kept = [
        (row, levels.get(key, 0))  # type: ignore[arg-type]
        for row in rows
        if (key := (row.get("media_type"), row.get("tmdb_id"))) not in seen
    ]
    shown = [(row, level) for row, level in kept if level != STRONG_NO]
    return [row for row, _ in sorted(shown, key=lambda pair: pair[1] < 0)]


def find_user_id(conn: sqlite3.Connection, username: str) -> int | None:
    row = conn.execute("SELECT id FROM admin_users WHERE username = ?", (username,)).fetchone()
    return row["id"] if row else None


def set_level(
    conn: sqlite3.Connection, user_id: int, title: RatedTitle, level: int, now: datetime
) -> int | None:
    """Rate *title* or change its rating; returns the level it replaced. A name already held is
    kept when *title* has none."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT level FROM user_ratings WHERE user_id = ? AND media_type = ? AND tmdb_id = ?",
            (user_id, title.media_type, title.tmdb_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO user_ratings"
            " (user_id, media_type, tmdb_id, level, title, year, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (user_id, media_type, tmdb_id) DO UPDATE SET level = excluded.level,"
            " title = COALESCE(excluded.title, title), year = COALESCE(excluded.year, year),"
            " updated_at = excluded.updated_at",
            (
                user_id,
                title.media_type,
                title.tmdb_id,
                level,
                title.title,
                title.year,
                _iso(now),
            ),
        )
    return row["level"] if row else None


def clear_level(conn: sqlite3.Connection, user_id: int, title: RatedTitle) -> int | None:
    """Take the rating of *title* back; returns the level it had."""
    with conn:
        rows = conn.execute(
            "DELETE FROM user_ratings WHERE user_id = ? AND media_type = ? AND tmdb_id = ?"
            " RETURNING level",
            (user_id, title.media_type, title.tmdb_id),
        ).fetchall()
    return rows[0]["level"] if rows else None


def claim_ask(
    conn: sqlite3.Connection, user_id: int, title: RatedTitle, season: int, now: datetime
) -> bool:
    """True the first time a person is to be asked of a film (*season* 0) or a season, and never
    of a title they rated."""
    with conn:
        # One statement, so two players at the end at once ask one time.
        done = conn.execute(
            "INSERT INTO user_rating_asks (user_id, media_type, tmdb_id, season_number, asked_at)"
            " SELECT ?1, ?2, ?3, ?4, ?5 WHERE NOT EXISTS (SELECT 1 FROM user_ratings"
            " WHERE user_id = ?1 AND media_type = ?2 AND tmdb_id = ?3)"
            " ON CONFLICT DO NOTHING",
            (user_id, title.media_type, title.tmdb_id, season, _iso(now)),
        )
    return done.rowcount == 1


def library_titles(
    conn: sqlite3.Connection, libraries: Sequence[str], refs: Iterable[str]
) -> dict[str, RatedTitle | None]:
    """The titles of the library refs ("movie-<key>", "show-<key>") among *refs* that the scanned
    libraries hold; None for one whose TMDB id is not known."""
    split = [ref.partition("-") for ref in refs]
    rows = conn.execute(
        "WITH libs AS (SELECT CAST(value AS TEXT) AS id FROM json_each(:libraries)),"
        " held AS (SELECT * FROM media_items"
        " WHERE CAST(plex_library_id AS TEXT) IN (SELECT id FROM libs))"
        " SELECT 'movie-' || id AS ref, 'movie' AS media_type, tmdb_id, title, year, file_path"
        " FROM held WHERE media_type = 'movie' AND id IN (SELECT value FROM json_each(:movies))"
        " UNION ALL"
        " SELECT 'show-' || show_key, 'tv', COALESCE(tmdb, (SELECT m.tmdb_id FROM title_meta m"
        " WHERE m.id_type = 'tvdb' AND m.id = tvdb)), name, year, file_path FROM ("
        "SELECT show_rating_key AS show_key, NULLIF(MAX(tmdb_id), 0) AS tmdb, MAX(tvdb_id) AS tvdb,"
        " MAX(COALESCE(show_title, title)) AS name, MAX(year) AS year, MIN(file_path) AS file_path"
        " FROM held WHERE show_rating_key IN (SELECT value FROM json_each(:shows))"
        " GROUP BY show_rating_key)",
        {
            "libraries": json.dumps(list(libraries)),
            "movies": json.dumps([key for kind, _, key in split if kind == "movie"]),
            "shows": json.dumps([key for kind, _, key in split if kind == "show"]),
        },
    )
    return {
        r["ref"]: RatedTitle(
            r["media_type"], r["tmdb_id"], r["title"], r["year"] or folder_year(r["file_path"])
        )
        if r["tmdb_id"] and r["tmdb_id"] > 0
        else None
        for r in rows
    }


def season_end(conn: sqlite3.Connection, episode_key: str, show_key: str) -> SeasonEnd | None:
    """Where an episode stands in its season, or None when the library sync does not list it."""
    row = conn.execute(
        "SELECT mi.season_number AS season, json_extract(e.value, '$.number') AS episode,"
        " (SELECT MAX(json_extract(o.value, '$.number')) FROM json_each(mi.episode_keys) o)"
        " AS final FROM media_items mi, json_each(mi.episode_keys) e"
        " WHERE mi.show_rating_key = ? AND mi.season_number > 0"
        " AND json_extract(e.value, '$.key') = ?",
        (show_key, episode_key),
    ).fetchone()
    if row is None or row["episode"] is None:
        return None
    return SeasonEnd(show_key, row["season"], row["episode"], row["episode"] >= row["final"])


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()
