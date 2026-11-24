"""The genres, dates and main cast TMDB gives the library's own titles, kept in ``title_meta``.

A scheduled job fills the table a few titles a minute, from the details already cached or
from TMDB, and never from Plex; nothing is fetched while a page waits. A title stays out
of every answer until it is filled, and leaves with its library title.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
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

from ._discover import TitleNotFound, _failure, details
from ._explore import _AS_THEMSELVES, _TOP_RATED_VOTES, _mixed
from ._gate import TmdbBusy, TmdbDown
from ._genres import GENRES, Genre
from ._shape import tmdb_id_of, tmdb_image_url

logger = logging.getLogger(__name__)

ASKS_PER_RUN = 6
_TITLES_PER_RUN = 200
_PAUSE_S = 15 * 60.0
_TIMEOUT_S = 5.0
_TRIES = 3
_TOKEN_RETRY_S = 60 * 60.0
_REFRESH = timedelta(days=30)
# TMDB may have a new show days after the library does.
_REFRESH_ABSENT = timedelta(days=7)
_REFRESH_FAILED = timedelta(days=1)
# A title just added must not take what the rest of the library gives off the page.
_READY_SHARE = 0.9
# A share of a small library is less than one title.
_READY_MISSING = 2

_HELD_S = 60.0

LIBRARY_PAGE = 40
PEOPLE_IN_LIBRARY = 6
ACTORS_OF_GENRE = 3
# One title says little of what someone is often in.
_OFTEN = 2

_clock = time.monotonic
_resume_at = 0.0
# The token TMDB was left alone with: a new one is tried at once.
_paused_token: str | None = None
# Titles TMDB failed on: how many asks, and when the next may come.
_failed: dict[tuple[str, int], tuple[int, float]] = {}
# The title TMDB was last down for, and whether that counted as an ask of its own; None once
# TMDB has answered since.
_down: tuple[tuple[str, int], bool] | None = None
# (stored value, when it may be tried again).
_unreadable_token: tuple[str, float] | None = None

_lock = threading.Lock()
# Held while building, so callers that find nothing held build it once.
_build_lock = threading.Lock()
# Bumped when cleared, so what was built before is not held after.
_generation = 0
# (database file, libraries) -> (held until, the library); one entry at most.
_library: dict[tuple[str, str], tuple[float, _Library]] = {}


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
    is due. The run ends when its asks of TMDB are spent or *stopped* says so; when TMDB is
    down, busy or refuses the token, or every title asked for failed, it is left alone for a
    while."""
    with conn:
        dropped = conn.execute(
            "DELETE FROM title_meta WHERE (id_type, id) NOT IN"
            f" (SELECT id_type, id FROM ({_ALL_TITLES}))"
        ).rowcount
    if dropped:
        clear_held()
    client = _client(conn, secret_key)
    if client is None or (client.token == _paused_token and _clock() < _resume_at):
        return
    if _fill_due(conn, client, stopped):
        clear_held()


def _client(conn: sqlite3.Connection, secret_key: str) -> TmdbClient | None:
    """None without a token, or with one that cannot be decrypted: that is tried
    once an hour, since every try logs a warning."""
    global _unreadable_token
    row = conn.execute("SELECT value FROM settings WHERE key = 'tmdb_read_token'").fetchone()
    token = row["value"] if row else None
    unreadable, until = _unreadable_token or (None, 0.0)
    if token is not None and token == unreadable and _clock() < until:
        return None
    client = TmdbClient.from_db(conn, secret_key, timeout=_TIMEOUT_S, retry=False)
    _unreadable_token = None
    if client is None and token is not None:
        _unreadable_token = (token, _clock() + _TOKEN_RETRY_S)
    return client


def _fill_due(conn: sqlite3.Connection, client: TmdbClient, stopped: Callable[[], bool]) -> int:
    """How many titles were settled."""
    global _resume_at, _paused_token, _down
    asked = settled = failures = answered = 0

    def allow_fetch() -> bool:
        nonlocal asked
        if asked == ASKS_PER_RUN:
            return False
        asked += 1
        return True

    for key in _due(conn)[:_TITLES_PER_RUN]:
        if stopped():
            break
        before = asked
        try:
            _fill_one(conn, client, key, allow_fetch)
        except disk_cache.Refused:
            break
        except sqlite3.Error:
            raise
        # rationale: whatever one title brings up must not end every later run on it.
        except Exception as exc:
            own = asked > before
            if own and _is_outage(exc, key):
                _resume_at, _paused_token = _clock() + _PAUSE_S, client.token
                # Last from now on, should it be this title alone TMDB fails on.
                _failed[key] = (_failed.get(key, (0, 0.0))[0], _resume_at)
                break
            logger.info("watch: a library title's details could not be read", exc_info=True)
            failures += own
            settled += _note_failure(conn, key, own)
            if own and not isinstance(exc, TmdbDown):
                _down = None
        else:
            _failed.pop(key, None)
            settled += 1
            if asked > before:
                answered += 1
                _down = None
    if failures and not answered:
        # Every title alike is not the titles' fault.
        _resume_at, _paused_token = _clock() + _PAUSE_S, client.token
    return settled


def _is_outage(exc: Exception, key: tuple[str, int]) -> bool:
    """Whether TMDB is to be left alone after failing on *key*. Down for one title may be
    that title's own fault; for another next, it is not."""
    global _down
    if isinstance(exc, TmdbBusy):
        return True
    if isinstance(exc, TmdbDown):
        before, _down = _down, (key, True)
        if before is None or before[0] == key:
            return False
        _down = (key, False)
        other, counted = before
        if counted and other in _failed:
            tries, again = _failed[other]
            _failed[other] = (tries - 1, again)
        return True
    cause = exc.__cause__
    return isinstance(cause, SafeHTTPError) and cause.status_code in (401, 403)


def _note_failure(conn: sqlite3.Connection, key: tuple[str, int], asked: bool) -> bool:
    """Leave the title for a while, and after a few asks of its own for a day;
    what is held of it stays. Whether it is settled so."""
    tries = _failed.get(key, (0, 0.0))[0] + asked
    if tries < _TRIES:
        _failed[key] = (tries, _clock() + _PAUSE_S)
        return False
    _failed.pop(key, None)
    # Dated back so far that it is due again in a day.
    due_again = now_utc() + _REFRESH_FAILED
    absent, held = (due_again - _REFRESH_ABSENT), (due_again - _REFRESH)
    with conn:
        conn.execute(
            "INSERT INTO title_meta (id_type, id, fetched_at) VALUES (?, ?, ?)"
            " ON CONFLICT(id_type, id) DO UPDATE SET fetched_at ="
            " CASE WHEN details IS NULL THEN excluded.fetched_at ELSE ? END",
            (*key, absent.isoformat(), held.isoformat()),
        )
    return True


def _due(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """The titles to ask for, without those that just failed: those that failed before last, those never filled first, then the
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
    now_s = _clock()
    waiting = {key for key, (_, again) in _failed.items() if again > now_s}
    due.sort(key=lambda title: (title[0] in _failed, title[1], title[0]))
    return [key for key, _ in due if key not in waiting]


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
        try:
            found = client.find_by_tvdb(title_id)
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            raise _failure(exc, "find") from exc
        tmdb_id = tmdb_id_of({"id": found})
    fields = None
    if tmdb_id is not None:
        with contextlib.suppress(TitleNotFound):
            fields = details(
                client,
                kind,
                tmdb_id,
                allow_fetch=allow_fetch,
                brief=True,
                fresh=True,
                background=True,
            )
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
    # The cached answer may be older than the list of genres.
    named = [GENRES[g["genre"]] for g in fields["genre_refs"] if g["genre"] in GENRES]
    ids = {g.tmdb_id(kind) for g in named}
    return {
        "title": fields["title"],
        "released": fields["release_date"] or "",
        "poster": _image_path(fields["poster_url"]),
        "rating": fields["tmdb_rating"],
        "votes": fields["vote_count"],
        "popularity": fields["popularity"],
        # A wide TV genre is several of those here.
        "genres": [g.id for g in GENRES.values() if g.tmdb_id(kind) in ids],
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
        missing = self.total - self.known
        return self.known > 0 and (
            missing <= _READY_MISSING or self.known >= self.total * _READY_SHARE
        )


def coverage(conn: sqlite3.Connection) -> Coverage:
    row = conn.execute(
        "SELECT COUNT(m.id) AS known, COUNT(*) AS total"
        f" FROM (SELECT DISTINCT id_type, id FROM ({_SELECTED_TITLES}))"
        " LEFT JOIN title_meta m USING (id_type, id)"
    ).fetchone()
    return Coverage(row["known"], row["total"])


def _held(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The filled titles of the selected libraries, each TMDB title once."""
    return conn.execute(
        "SELECT MIN(t.ref) AS ref, CASE t.id_type WHEN 'movie' THEN 'movie' ELSE 'tv' END AS kind,"
        " m.tmdb_id AS tmdb_id, m.details AS details, m.cast_json AS cast_json"
        f" FROM ({_SELECTED_TITLES}) t JOIN title_meta m USING (id_type, id)"
        " WHERE m.details IS NOT NULL GROUP BY kind, m.tmdb_id"
    ).fetchall()


def clear_held() -> None:
    global _generation
    with _lock:
        _library.clear()
        _generation += 1


def _current(conn: sqlite3.Connection) -> _Library:
    """What is read of the table, held a while: parsing it takes too long for each request."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'plex_libraries'").fetchone()
    key = (
        str(conn.execute("PRAGMA database_list").fetchone()[2]),
        str(row["value"] if row else ""),
    )
    with _build_lock:
        with _lock:
            held = _library.get(key)
            generation = _generation
        if held is not None and held[0] > _clock() and _all_there(conn, held[1]):
            return held[1]
        built = _build(conn)
        with _lock:
            if generation == _generation:
                _library.clear()
                _library[key] = (_clock() + _HELD_S, built)
        return built


def _all_there(conn: sqlite3.Connection, library: _Library) -> bool:
    there = {row["ref"] for row in conn.execute(_SELECTED_TITLES)}
    return all(title["library_ref"] in there for title in library.titles)


def _build(conn: sqlite3.Connection) -> _Library:
    items = []
    casts = []
    for row in _held(conn):
        kept = json.loads(row["details"])
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
                "genres": kept["genres"],
            }
        )
        casts.append((kept["genres"], json.loads(row["cast_json"])))
    return _Library(items, _people(coverage(conn), casts))


def library_titles(
    conn: sqlite3.Connection,
    genre: Genre,
    *,
    media_type: str = "all",
    sort: str = "popular",
    decade: str | None = None,
) -> tuple[list[dict[str, Any]], Coverage]:
    """The library's titles of *genre* in order, with the fields ``tmdb_card`` reads and
    their ``library_ref``, and how far the table is filled."""
    library = _current(conn)
    items = [
        i
        for i in library.titles
        if genre.id in i["genres"]
        and media_type in ("all", i["media_type"])
        and (decade is None or i["release_date"][:3] == decade[:3])
    ]
    return _ordered(items, sort), library.people.coverage


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


@dataclass(frozen=True, slots=True)
class _Library:
    titles: list[dict[str, Any]]
    people: LibraryPeople


def library_people(conn: sqlite3.Connection) -> LibraryPeople:
    """Nobody while the table is not filled: the counts would be wrong."""
    return _current(conn).people


def _people(covered: Coverage, casts: Iterable[tuple[list[str], list[Any]]]) -> LibraryPeople:
    if not covered.ready:
        return LibraryPeople(covered)
    everyone: dict[int, _Part] = {}
    by_genre: dict[str, dict[int, _Part]] = defaultdict(dict)
    for genres, cast in casts:
        lists = [everyone, *(by_genre[g] for g in genres)]
        for place, (person_id, name, path) in enumerate(cast):
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
