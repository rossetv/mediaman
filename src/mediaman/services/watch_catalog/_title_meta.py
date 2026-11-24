"""The genres, dates and main cast TMDB gives the library's own titles, kept in ``title_meta``.

A scheduled job fills the table a few titles a minute, from the details already cached or
from TMDB, and never from Plex; nothing is fetched while a page waits. A title stays out
of every answer until it is filled, and leaves with its library title.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests

from mediaman.core.library_selected import LIBRARY_SELECTED
from mediaman.core.time import now_utc
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient

from ._discover import TitleNotFound, TmdbUnavailable, details
from ._explore import _AS_THEMSELVES, _TOP_RATED_VOTES, _mixed
from ._genres import GENRES, Genre
from ._shape import tmdb_image_url

ASKS_PER_RUN = 6
_TITLES_PER_RUN = 200
_PAUSE_S = 15 * 60.0
_TIMEOUT_S = 5.0
_REFRESH = timedelta(days=30)
# TMDB may have a new show days after the library does.
_REFRESH_ABSENT = timedelta(days=7)
# A title just added must not take what the rest of the library gives off the page.
_READY_SHARE = 0.9

LIBRARY_PAGE = 40
PEOPLE_IN_LIBRARY = 6
ACTORS_OF_GENRE = 3
# One title says little of what someone is often in.
_OFTEN = 2

_clock = time.monotonic
_resume_at = 0.0
# Titles TMDB failed on, left for last so that one it cannot answer holds up no other.
_failed: set[tuple[str, int]] = set()


def _titles_sql(where: str) -> str:
    return (
        "SELECT 'movie-' || mi.id AS ref, 'movie' AS id_type, mi.tmdb_id AS id"
        f" FROM media_items mi WHERE mi.media_type = 'movie' AND mi.tmdb_id > 0 AND {where}"
        " UNION ALL SELECT 'show-' || show_key, CASE WHEN tmdb > 0 THEN 'tv' ELSE 'tvdb' END,"
        " CASE WHEN tmdb > 0 THEN tmdb ELSE tvdb END FROM ("
        "SELECT mi.show_rating_key AS show_key, MAX(mi.tmdb_id) AS tmdb, MAX(mi.tvdb_id) AS tvdb"
        f" FROM media_items mi WHERE mi.show_rating_key IS NOT NULL AND {where}"
        " GROUP BY mi.show_rating_key) WHERE tmdb > 0 OR tvdb > 0"
    )


_ALL_TITLES = _titles_sql("1")
_SELECTED_TITLES = _titles_sql(LIBRARY_SELECTED)


def fill_title_meta(
    conn: sqlite3.Connection, secret_key: str, *, stopped: Callable[[], bool]
) -> None:
    """One run of the job: drop what the library no longer holds, then fill or refresh what
    is due. The run ends when its asks of TMDB are spent or *stopped* says so, and after a
    failure TMDB is left alone for a while."""
    global _resume_at
    with conn:
        conn.execute(
            "DELETE FROM title_meta WHERE (id_type, id) NOT IN"
            f" (SELECT id_type, id FROM ({_ALL_TITLES}))"
        )
    if _clock() < _resume_at:
        return
    client = TmdbClient.from_db(conn, secret_key, timeout=_TIMEOUT_S, retry=False)
    if client is None:
        return
    asks = iter(range(ASKS_PER_RUN))

    def allow_fetch() -> bool:
        return next(asks, None) is not None

    for key in _due(conn)[:_TITLES_PER_RUN]:
        if stopped():
            return
        try:
            _fill_one(conn, client, key, allow_fetch)
        except disk_cache.Refused:
            return
        except (TmdbUnavailable, SafeHTTPError, requests.RequestException, ValueError):
            _failed.add(key)
            _resume_at = _clock() + _PAUSE_S
            return
        _failed.discard(key)


def _due(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """The titles to ask for: those that failed last, those never filled first, then the
    oldest."""
    now = now_utc()
    rows = conn.execute(
        "SELECT DISTINCT t.id_type, t.id, m.fetched_at, m.details IS NULL AS absent"
        f" FROM ({_SELECTED_TITLES}) t LEFT JOIN title_meta m USING (id_type, id)"
    ).fetchall()
    due = [
        ((r["id_type"], r["id"]), r["fetched_at"] or "")
        for r in rows
        if r["fetched_at"] is None
        or now - datetime.fromisoformat(r["fetched_at"])
        >= (_REFRESH_ABSENT if r["absent"] else _REFRESH)
    ]
    due.sort(key=lambda title: (title[0] in _failed, title[1], title[0]))
    return [key for key, _ in due]


def _fill_one(
    conn: sqlite3.Connection,
    client: TmdbClient,
    key: tuple[str, int],
    allow_fetch: Callable[[], bool],
) -> None:
    id_type, title_id = key
    kind = "movie" if id_type == "movie" else "tv"
    tmdb_id: int | None = title_id
    if id_type == "tvdb":
        if not allow_fetch():
            raise disk_cache.Refused(str(title_id))
        tmdb_id = client.find_by_tvdb(title_id)
    fields = None
    if tmdb_id is not None:
        with contextlib.suppress(TitleNotFound):
            fields = details(client, kind, tmdb_id, allow_fetch=allow_fetch, brief=True, fresh=True)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO title_meta"
            " (id_type, id, tmdb_id, details, cast_json, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                id_type,
                title_id,
                tmdb_id,
                json.dumps(_kept(fields, kind)) if fields else None,
                json.dumps(_kept_cast(fields)) if fields else None,
                now_utc().isoformat(),
            ),
        )


def _kept(fields: Mapping[str, Any], kind: str) -> dict[str, Any]:
    ids = {GENRES[g["genre"]].tmdb_id(kind) for g in fields["genre_refs"]}
    return {
        "title": fields["title"],
        "released": fields["release_date"] or "",
        "poster": _image_path(fields["poster_url"]),
        "rating": fields["tmdb_rating"],
        "votes": fields["vote_count"],
        "popularity": fields["popularity"],
        # A wide TV genre is several of those here.
        "genres": [g.id for g in GENRES.values() if g.tmdb_id(kind) in ids - {None}],
    }


def _kept_cast(fields: Mapping[str, Any]) -> list[list[Any]]:
    kept: dict[int, list[Any]] = {}
    for c in fields["cast"]:
        if c["person_id"] and c["name"] and not _AS_THEMSELVES.match(c["character"]):
            # Someone with two parts stands in the cast twice.
            kept.setdefault(
                c["person_id"], [c["person_id"], c["name"], _image_path(c["profile_url"])]
            )
    return list(kept.values())


def _image_path(url: str | None) -> str | None:
    return "/" + url.rpartition("/")[2] if url else None


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much of the library is filled: of its *total* titles with an id, the *known*."""

    known: int
    total: int

    @property
    def ready(self) -> bool:
        return self.known > 0 and self.known >= self.total * _READY_SHARE


def coverage(conn: sqlite3.Connection) -> Coverage:
    row = conn.execute(
        "SELECT COUNT(m.id) AS known, COUNT(*) AS total"
        f" FROM (SELECT DISTINCT id_type, id FROM ({_SELECTED_TITLES}))"
        " LEFT JOIN title_meta m USING (id_type, id)"
    ).fetchone()
    return Coverage(row["known"], row["total"])


def _held(conn: sqlite3.Connection, column: str = "NULL") -> list[sqlite3.Row]:
    """The filled titles of the selected libraries, each TMDB title once."""
    return conn.execute(
        "SELECT MIN(t.ref) AS ref, CASE t.id_type WHEN 'movie' THEN 'movie' ELSE 'tv' END AS kind,"
        f" m.tmdb_id AS tmdb_id, m.details AS details, {column} AS cast_json"
        f" FROM ({_SELECTED_TITLES}) t JOIN title_meta m USING (id_type, id)"
        " WHERE m.details IS NOT NULL GROUP BY kind, m.tmdb_id"
    ).fetchall()


def library_titles(
    conn: sqlite3.Connection,
    genre: Genre,
    *,
    media_type: str = "all",
    sort: str = "popular",
    decade: str | None = None,
) -> tuple[list[dict[str, Any]], Coverage]:
    """The library's titles of *genre* in order, as the arguments of a card's ``id``,
    ``media_type``, ``library_ref`` and TMDB fields, and how far the table is filled."""
    items = []
    for row in _held(conn):
        kept = json.loads(row["details"])
        if (
            genre.id not in kept["genres"]
            or media_type not in ("all", row["kind"])
            or (decade is not None and kept["released"][:3] != decade[:3])
        ):
            continue
        items.append(
            {
                "id": row["tmdb_id"],
                "media_type": row["kind"],
                "library_ref": row["ref"],
                "title": kept["title"],
                "release_date": kept["released"],
                "poster_path": kept["poster"],
                "vote_average": kept["rating"],
                "votes": kept["votes"],
                "popularity": kept["popularity"],
            }
        )
    return _ordered(items, sort), coverage(conn)


def _ordered(items: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    items.sort(key=lambda i: (i["title"].casefold(), i["id"]))
    if sort == "newest":
        return sorted(items, key=lambda i: i["release_date"], reverse=True)
    if sort == "top_rated":
        # Held titles are all listed: one with few votes stands after those with enough.
        return sorted(
            items,
            key=lambda i: (
                i["votes"] < _TOP_RATED_VOTES[i["media_type"]],
                -(i["vote_average"] or 0),
            ),
        )
    items.sort(key=lambda i: -i["popularity"])
    return _mixed([[i for i in items if i["media_type"] == kind] for kind in ("movie", "tv")], sort)


@dataclass(slots=True)
class _Part:
    name: str
    path: str | None
    titles: int = 0
    billing: int = 0


@dataclass(frozen=True, slots=True)
class LibraryPeople:
    """The people with most titles in the library, and of each genre by its id."""

    coverage: Coverage
    people: list[dict[str, Any]] = field(default_factory=list)
    actors: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def library_people(conn: sqlite3.Connection) -> LibraryPeople:
    """Nobody while the table is not filled: the counts would be wrong."""
    covered = coverage(conn)
    if not covered.ready:
        return LibraryPeople(covered)
    everyone: dict[int, _Part] = {}
    by_genre: dict[str, dict[int, _Part]] = defaultdict(dict)
    for row in _held(conn, "m.cast_json"):
        lists = [everyone, *(by_genre[g] for g in json.loads(row["details"])["genres"])]
        for place, (person_id, name, path) in enumerate(json.loads(row["cast_json"])):
            for found in lists:
                part = found.setdefault(person_id, _Part(name, path))
                part.titles += 1
                part.billing += place
    return LibraryPeople(
        covered,
        _most_seen(everyone.items(), PEOPLE_IN_LIBRARY),
        {g: _most_seen(found.items(), ACTORS_OF_GENRE) for g, found in by_genre.items()},
    )


def _most_seen(parts: Iterable[tuple[int, _Part]], limit: int) -> list[dict[str, Any]]:
    often = [p for p in parts if p[1].titles >= _OFTEN]
    often.sort(key=lambda p: (-p[1].titles, p[1].billing, p[1].name, p[0]))
    return [
        {
            "person_id": person_id,
            "name": part.name,
            "profile_url": tmdb_image_url(part.path, "w185"),
            "titles": part.titles,
        }
        for person_id, part in often[:limit]
    ]
