"""Repository functions for the films and shows each person marked watched, by TMDB id.

Every read is of one person's own marks. Nothing that scans, deletes or keeps titles reads
them, and nothing here is written to the watch progress it does read.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    not_seen: frozenset[str]  # what of it was taken back: seasons ("2") and episodes ("2.5")
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
    # The shows of them of which a part was taken back.
    partly: set[TitleKey] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class CoveredSeason:
    """A season of a library show that a person's mark of the show covers."""

    season_id: str
    show_key: str
    episodes: frozenset[str]  # those seen by the mark alone
    whole: bool  # nothing of it is left unseen
    seen_at: str


@dataclass(frozen=True, slots=True)
class ShowPart:
    """A regular season of a library show, or one episode of it. *episode* is None for the
    season, and for an episode Plex has no number of."""

    show_key: str
    season: int
    episode: int | None

    @property
    def name(self) -> str:
        return str(self.season) if self.episode is None else f"{self.season}.{self.episode}"


def person_marks(conn: sqlite3.Connection, username: str) -> PersonMarks:
    found = PersonMarks()
    for r in conn.execute(
        "SELECT media_type, tmdb_id, level, NULL AS through, NULL AS not_seen, 0 AS seen"
        " FROM user_ratings"
        " WHERE user_id = (SELECT id FROM admin_users WHERE username = :name)"
        " UNION ALL SELECT media_type, tmdb_id, NULL, through_season, not_seen, 1"
        " FROM user_seen_titles"
        " WHERE user_id = (SELECT id FROM admin_users WHERE username = :name)",
        {"name": username},
    ):
        key = (r["media_type"], r["tmdb_id"])
        if r["seen"]:
            found.seen[key] = r["through"]
            if r["not_seen"]:
                found.partly.add(key)
        else:
            found.levels[key] = r["level"]
    return found


def get_mark(conn: sqlite3.Connection, user_id: int, key: TitleKey) -> SeenMark | None:
    row = conn.execute(
        "SELECT through_season, not_seen, in_library, title, year, seen_at FROM user_seen_titles"
        " WHERE user_id = ? AND media_type = ? AND tmdb_id = ?",
        (user_id, *key),
    ).fetchone()
    if row is None:
        return None
    return SeenMark(
        row["through_season"],
        _parts(row["not_seen"]),
        bool(row["in_library"]),
        row["title"],
        row["year"],
        row["seen_at"],
    )


def _parts(stored: str | None) -> frozenset[str]:
    return frozenset(json.loads(stored or "[]"))


def mark(
    conn: sqlite3.Connection,
    user_id: int,
    title: RatedTitle,
    through_season: int | None,
    now: datetime,
    *,
    in_library: bool,
) -> SeenChange:
    """Mark *title* watched whole, a show up to *through_season* or the season it is marked up
    to already if later. A name already held is kept when *title* has none."""
    key = (title.media_type, title.tmdb_id)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        before = get_mark(conn, user_id, key)
        if before is not None and through_season is not None:
            through_season = max(through_season, before.through_season or 0)
        after = SeenMark(
            through_season,
            frozenset(),
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


def change_part(
    conn: sqlite3.Connection, user_id: int, key: TitleKey, part: ShowPart, *, seen: bool
) -> SeenChange | None:
    """Take a part of a marked show back, or give back with *seen* what was; None when the mark
    does not reach it or it is so already."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        before = get_mark(conn, user_id, key)
        if before is None or part.season > (before.through_season or 0):
            return None
        if seen:
            inside = f"{part.name}."
            parts = {p for p in before.not_seen if p != part.name and not p.startswith(inside)}
        else:
            parts = {*before.not_seen, part.name}
        if parts == before.not_seen:
            return None
        after = replace(before, not_seen=frozenset(parts))
        _write(conn, user_id, key, after)
    return SeenChange(user_id, key, before, after)


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
        " not_seen, in_library, title, year, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            *key,
            mark.through_season,
            json.dumps(sorted(mark.not_seen)) if mark.not_seen else None,
            int(mark.in_library),
            mark.title,
            mark.year,
            mark.seen_at,
        ),
    )


def library_refs(conn: sqlite3.Connection, libraries: Sequence[str], key: TitleKey) -> list[str]:
    """The refs ("movie-<key>", "show-<key>") of the title's copies in the scanned libraries."""
    media_type, tmdb_id = key
    params = {"libraries": json.dumps(list(libraries)), "tmdb_id": tmdb_id}
    if media_type == "movie":
        rows = conn.execute(
            "SELECT 'movie-' || id AS ref FROM media_items WHERE media_type = 'movie'"
            " AND tmdb_id = :tmdb_id AND CAST(plex_library_id AS TEXT) IN"
            " (SELECT CAST(value AS TEXT) FROM json_each(:libraries)) ORDER BY id",
            params,
        )
    else:
        # rationale: _SHOWS is a fixed string; every value is bound
        rows = conn.execute(  # nosec B608
            f"{_SHOWS} SELECT 'show-' || show_key AS ref FROM shows WHERE tmdb_id = :tmdb_id"
            " ORDER BY show_key",
            params,
        )
    return [r["ref"] for r in rows]


def seen_films(conn: sqlite3.Connection, username: str, libraries: Sequence[str]) -> set[str]:
    """The keys of the library's films that *username*'s marks of them as titles cover. Of a
    mark made in the library, a copy with a row of its own says for itself."""
    rows = conn.execute(
        "SELECT mi.id FROM user_seen_titles t JOIN admin_users u ON u.id = t.user_id"
        " JOIN media_items mi ON mi.media_type = 'movie' AND mi.tmdb_id = t.tmdb_id"
        " WHERE u.username = :name AND t.media_type = 'movie'"
        " AND CAST(mi.plex_library_id AS TEXT) IN"
        " (SELECT CAST(value AS TEXT) FROM json_each(:libraries))"
        " AND NOT (t.in_library AND EXISTS (SELECT 1 FROM watch_progress wp"
        " WHERE wp.username = :name AND wp.rating_key = mi.id))",
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
    the shows cover: those up to the season a show is marked up to, less what was taken back
    and the episodes begun and not seen. A mark made in the library covers no season it left
    rows on: those say which episodes were meant."""
    # rationale: _SHOWS is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        f"{_SHOWS} SELECT held.id, held.show_rating_key, held.season_number, held.episode_keys,"
        " t.seen_at, t.in_library, t.not_seen"
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
    seasons = [(r, _episodes(r["episode_keys"])) for r in rows]
    by_episode: dict[str, sqlite3.Row] = {}
    by_season: dict[str, list[sqlite3.Row]] = {}
    for p in conn.execute(
        "SELECT rating_key, media_item_id, watched, seen_at, updated_at FROM watch_progress"
        " WHERE username = :name AND (rating_key IN (SELECT value FROM json_each(:episodes))"
        " OR media_item_id IN (SELECT value FROM json_each(:seasons)))",
        {
            "name": username,
            "episodes": json.dumps([key for _, listed in seasons for key in listed or ()]),
            "seasons": json.dumps([r["id"] for r, listed in seasons if listed is None]),
        },
    ):
        by_episode[p["rating_key"]] = p
        by_season.setdefault(p["media_item_id"], []).append(p)
    found = []
    for r, listed in seasons:
        season, taken_back = r["season_number"], _parts(r["not_seen"])
        held = (
            by_season.get(r["id"], [])
            if listed is None
            else [by_episode[key] for key in listed if key in by_episode]
        )
        if str(season) in taken_back or (
            r["in_library"] and any(_was_there(p, r["seen_at"]) for p in held)
        ):
            continue
        own = {p["rating_key"]: bool(p["watched"] or p["seen_at"]) for p in held if _says(p)}
        episodes = frozenset(
            key
            for key, number in (listed or {}).items()
            if key not in own and f"{season}.{number}" not in taken_back
        )
        whole = (
            all(own.values())
            if listed is None
            else all(key in episodes or own.get(key) for key in listed)
        )
        found.append(CoveredSeason(r["id"], r["show_rating_key"], episodes, whole, r["seen_at"]))
    return found


def _says(row: sqlite3.Row) -> bool:
    """Whether a row says more than that its mark was taken back."""
    return bool(row["watched"] or row["seen_at"] or row["updated_at"] > NEVER_PLAYED)


def _was_there(row: sqlite3.Row, marked_at: str) -> bool:
    """Whether a row was there when the show was marked."""
    if row["seen_at"] is not None:
        return bool(row["seen_at"] <= marked_at)
    return bool(row["watched"] or row["updated_at"] == NEVER_PLAYED)


def _episodes(stored: str | None) -> dict[str, int | None] | None:
    """The number of each episode of a season by key; None until a scan has listed them."""
    return None if stored is None else {str(e["key"]): e["number"] for e in json.loads(stored)}


def with_covered(
    conn: sqlite3.Connection,
    username: str,
    libraries: Sequence[str],
    show_key: str,
    progress: Mapping[str, WatchProgress],
) -> dict[str, WatchProgress]:
    """*username*'s *progress* on one show's episodes, with those seen by a mark of the show."""
    return dict(progress) | {
        key: WatchProgress(
            key, season.season_id, show_key, 0.0, 0.0, True, NEVER_PLAYED, season.seen_at
        )
        for season in covered_seasons(conn, username, libraries, show_key=show_key)
        for key in season.episodes
    }


def part_of(
    conn: sqlite3.Connection, libraries: Sequence[str], kind: str, key: str
) -> ShowPart | None:
    """The part of a library show that a "season" or "episode" is; None for a special."""
    if kind == "season":
        sql = (
            "SELECT show_rating_key, season_number, NULL AS number FROM held"
            " WHERE season_number > 0 AND id = :key"
        )
    else:
        # The key's own text first, which is far quicker to look for than to read each list.
        sql = (
            "SELECT show_rating_key, season_number, json_extract(e.value, '$.number') AS number"
            " FROM held, json_each(episode_keys) e WHERE season_number > 0"
            " AND instr(episode_keys, :key) AND json_extract(e.value, '$.key') = :key"
        )
    # rationale: *sql* is one of the two fixed strings above
    row = conn.execute(  # nosec B608
        f"{_SHOWS} {sql} LIMIT 1", {"libraries": json.dumps(list(libraries)), "key": key}
    ).fetchone()
    if row is None:
        return None
    return ShowPart(row["show_rating_key"], row["season_number"], row["number"])
