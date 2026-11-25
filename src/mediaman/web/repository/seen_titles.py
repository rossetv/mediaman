"""Repository functions for the films and shows each person marked watched, by TMDB id.

Every read is of one person's own marks. Nothing that scans, deletes or keeps titles reads
them, and nothing here is written to the watch progress it does read.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mediaman.web.repository.ratings import RatedTitle
from mediaman.web.repository.watch_progress import NEVER_PLAYED, WatchProgress

# (media type, TMDB id)
TitleKey = tuple[str, int]


# The shows of the scanned libraries, with the TMDB id each is known by.
_SHOWS = (
    "WITH held AS (SELECT * FROM media_items WHERE show_rating_key IS NOT NULL AND"
    " CAST(plex_library_id AS TEXT) IN"
    " (SELECT CAST(value AS TEXT) FROM json_each(:libraries))),"
    " shows AS (SELECT show_key, COALESCE(tmdb, (SELECT m.tmdb_id FROM title_meta m"
    " WHERE m.id_type = 'tvdb' AND m.id = tvdb)) AS tmdb_id FROM ("
    "SELECT show_rating_key AS show_key, NULLIF(MAX(tmdb_id), 0) AS tmdb, MAX(tvdb_id) AS tvdb"
    " FROM held GROUP BY show_rating_key))"
)


@dataclass(frozen=True, slots=True)
class SeenMark:
    through_season: int | None  # None for a film
    in_library: bool
    title: str | None
    year: int | None
    seen_at: str


@dataclass(frozen=True, slots=True)
class SeenChange:
    """What a person's mark of a title was, and what it became; None where there is none."""

    user_id: int
    key: TitleKey
    before: SeenMark | None
    after: SeenMark | None


@dataclass(frozen=True, slots=True)
class PersonMarks:
    """One person's own ratings and marks, by title."""

    levels: dict[TitleKey, int] = field(default_factory=dict)
    # The season a show is marked up to; None for a film.
    seen: dict[TitleKey, int | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CoveredSeason:
    """A season of a library show that a person's mark of the show covers."""

    season_id: str
    show_key: str
    episodes: tuple[str, ...] | None  # None until a scan has listed them
    unfinished: frozenset[str]  # episodes begun and not seen, which the mark does not cover
    seen_at: str


def person_marks(conn: sqlite3.Connection, username: str) -> PersonMarks:
    found = PersonMarks()
    for r in conn.execute(
        "SELECT media_type, tmdb_id, level, NULL AS through, 0 AS seen FROM user_ratings"
        " WHERE user_id = (SELECT id FROM admin_users WHERE username = :name)"
        " UNION ALL SELECT media_type, tmdb_id, NULL, through_season, 1 FROM user_seen_titles"
        " WHERE user_id = (SELECT id FROM admin_users WHERE username = :name)",
        {"name": username},
    ):
        key = (r["media_type"], r["tmdb_id"])
        if r["seen"]:
            found.seen[key] = r["through"]
        else:
            found.levels[key] = r["level"]
    return found


def get_mark(conn: sqlite3.Connection, user_id: int, key: TitleKey) -> SeenMark | None:
    row = conn.execute(
        "SELECT through_season, in_library, title, year, seen_at FROM user_seen_titles"
        " WHERE user_id = ? AND media_type = ? AND tmdb_id = ?",
        (user_id, *key),
    ).fetchone()
    if row is None:
        return None
    return SeenMark(
        row["through_season"], bool(row["in_library"]), row["title"], row["year"], row["seen_at"]
    )


def mark(
    conn: sqlite3.Connection,
    user_id: int,
    title: RatedTitle,
    through_season: int | None,
    now: datetime,
    *,
    in_library: bool,
) -> SeenChange:
    """Mark *title* watched, a show up to *through_season* or the season it is marked up to
    already if later. A name already held is kept when *title* has none."""
    key = (title.media_type, title.tmdb_id)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        before = get_mark(conn, user_id, key)
        if before is not None and through_season is not None:
            through_season = max(through_season, before.through_season or 0)
        after = SeenMark(
            through_season,
            in_library,
            title.title or (before.title if before else None),
            title.year or (before.year if before else None),
            now.astimezone(UTC).isoformat(),
        )
        _write(conn, user_id, key, after)
    return SeenChange(user_id, key, before, after)


def unmark(conn: sqlite3.Connection, user_id: int, title: RatedTitle) -> SeenChange:
    key = (title.media_type, title.tmdb_id)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        before = get_mark(conn, user_id, key)
        _write(conn, user_id, key, None)
    return SeenChange(user_id, key, before, None)


def restore(conn: sqlite3.Connection, change: SeenChange) -> None:
    """Put the mark back as it was before *change*, unless it was changed again since."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if get_mark(conn, change.user_id, change.key) == change.after:
            _write(conn, change.user_id, change.key, change.before)


def _write(conn: sqlite3.Connection, user_id: int, key: TitleKey, mark: SeenMark | None) -> None:
    if mark is None:
        conn.execute(
            "DELETE FROM user_seen_titles WHERE user_id = ? AND media_type = ? AND tmdb_id = ?",
            (user_id, *key),
        )
        return
    conn.execute(
        "INSERT OR REPLACE INTO user_seen_titles (user_id, media_type, tmdb_id, through_season,"
        " in_library, title, year, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            *key,
            mark.through_season,
            int(mark.in_library),
            mark.title,
            mark.year,
            mark.seen_at,
        ),
    )


def library_ref(conn: sqlite3.Connection, libraries: Sequence[str], key: TitleKey) -> str | None:
    """The ref ("movie-<key>", "show-<key>") of the title in the scanned libraries, if held."""
    media_type, tmdb_id = key
    params = {"libraries": json.dumps(list(libraries)), "tmdb_id": tmdb_id}
    if media_type == "movie":
        row = conn.execute(
            "SELECT 'movie-' || id AS ref FROM media_items WHERE media_type = 'movie'"
            " AND tmdb_id = :tmdb_id AND CAST(plex_library_id AS TEXT) IN"
            " (SELECT CAST(value AS TEXT) FROM json_each(:libraries)) ORDER BY id LIMIT 1",
            params,
        ).fetchone()
    else:
        # rationale: _SHOWS is a fixed string; every value is bound
        row = conn.execute(  # nosec B608
            f"{_SHOWS} SELECT 'show-' || show_key AS ref FROM shows WHERE tmdb_id = :tmdb_id"
            " ORDER BY show_key LIMIT 1",
            params,
        ).fetchone()
    return row["ref"] if row else None


def seen_films(conn: sqlite3.Connection, username: str, libraries: Sequence[str]) -> set[str]:
    """The keys of the library's films that *username* marked watched as titles."""
    rows = conn.execute(
        "SELECT mi.id FROM user_seen_titles t JOIN admin_users u ON u.id = t.user_id"
        " JOIN media_items mi ON mi.media_type = 'movie' AND mi.tmdb_id = t.tmdb_id"
        " WHERE u.username = :name AND t.media_type = 'movie'"
        " AND CAST(mi.plex_library_id AS TEXT) IN"
        " (SELECT CAST(value AS TEXT) FROM json_each(:libraries))",
        {"name": username, "libraries": json.dumps(list(libraries))},
    )
    return {r["id"] for r in rows}


def covered_seasons(
    conn: sqlite3.Connection,
    username: str,
    libraries: Sequence[str],
    *,
    show_key: str | None = None,
) -> list[CoveredSeason]:
    """The seasons of the library's shows (or only of *show_key*) that *username*'s marks of
    the shows cover: those up to the season a show is marked up to. A mark made in the library
    covers no season it left rows on: those say which episodes were meant."""
    # rationale: _SHOWS is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        f"{_SHOWS} SELECT held.id, held.show_rating_key, held.episode_keys, t.seen_at, t.in_library"
        " FROM user_seen_titles t JOIN admin_users u ON u.id = t.user_id"
        " JOIN shows ON shows.tmdb_id = t.tmdb_id"
        " JOIN held ON held.show_rating_key = shows.show_key"
        " AND held.season_number BETWEEN 1 AND t.through_season"
        " WHERE u.username = :name AND t.media_type = 'tv'"
        " AND (:show_key IS NULL OR shows.show_key = :show_key)",
        {"name": username, "libraries": json.dumps(list(libraries)), "show_key": show_key},
    ).fetchall()
    if not rows:
        return []
    by_episode: dict[str, sqlite3.Row] = {}
    by_season: dict[str, list[sqlite3.Row]] = {}
    for p in conn.execute(
        "SELECT rating_key, media_item_id, watched, seen_at, updated_at FROM watch_progress"
        " WHERE username = ?",
        (username,),
    ):
        by_episode[p["rating_key"]] = p
        by_season.setdefault(p["media_item_id"], []).append(p)
    found = []
    for r in rows:
        episodes = _episodes(r["episode_keys"])
        held = (
            by_season.get(r["id"], [])
            if episodes is None
            else [by_episode[key] for key in episodes if key in by_episode]
        )
        if r["in_library"] and any(
            p["watched"] if p["seen_at"] is None else p["seen_at"] <= r["seen_at"] for p in held
        ):
            continue
        unfinished = frozenset(
            p["rating_key"]
            for p in held
            if not p["watched"] and p["seen_at"] is None and p["updated_at"] > NEVER_PLAYED
        )
        found.append(
            CoveredSeason(r["id"], r["show_rating_key"], episodes, unfinished, r["seen_at"])
        )
    return found


def _episodes(stored: str | None) -> tuple[str, ...] | None:
    return None if stored is None else tuple(str(e["key"]) for e in json.loads(stored))


def with_covered(
    conn: sqlite3.Connection,
    username: str,
    libraries: Sequence[str],
    show_key: str,
    progress: Mapping[str, WatchProgress],
) -> dict[str, WatchProgress]:
    """*username*'s *progress* on one show's episodes, with those seen by a mark of the show."""
    found = dict(progress)
    for season in covered_seasons(conn, username, libraries, show_key=show_key):
        for key in season.episodes or ():
            row = found.get(key)
            if row is None or not (row.seen or row.played):
                found[key] = WatchProgress(
                    key, season.season_id, show_key, 0.0, 0.0, True, NEVER_PLAYED, season.seen_at
                )
    return found


def show_of(conn: sqlite3.Connection, libraries: Sequence[str], kind: str, key: str) -> str | None:
    """The key of the library show that a "season" or "episode" is of, a special's aside."""
    where = (
        "id = :key"
        if kind == "season"
        else "EXISTS (SELECT 1 FROM json_each(episode_keys) e"
        " WHERE json_extract(e.value, '$.key') = :key)"
    )
    # rationale: *where* is one of the two fixed fragments above
    row = conn.execute(  # nosec B608
        f"{_SHOWS} SELECT show_rating_key FROM held WHERE season_number > 0 AND {where} LIMIT 1",
        {"libraries": json.dumps(list(libraries)), "key": key},
    ).fetchone()
    return row["show_rating_key"] if row else None
