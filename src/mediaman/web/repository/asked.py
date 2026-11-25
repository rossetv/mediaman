"""What a person asked mediaman to download, and which of it they have not watched.

Every read is of one person's own requests and plays, for admins too. Whatever names another
person, or cannot be told to be theirs, is left out.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from mediaman.core.time import parse_iso_utc
from mediaman.services import watch_catalog as catalog
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.watch_progress import seen_movies

Kind = Literal["movie", "tv"]

# A request's audit row and its notification are written in one transaction, which may wait
# for the write lock in between.
_TOGETHER = timedelta(seconds=30)
_ID_RE = re.compile(r"(tmdb|tvdb):(\d{1,10})(?::s(\d{1,4}))?")


@dataclass(frozen=True, slots=True)
class Request:
    """One download a person asked for. *kind* is None when only a name of it is known."""

    kind: Kind | None
    title: str
    tmdb_id: int | None
    tvdb_id: int | None
    season: int | None  # None for the film or the whole show
    asked_at: datetime
    waiting: bool  # the email that it is there is still to be sent

    def names(self, kind: Kind, tmdb_id: int | None, tvdb_id: int | None, title: str) -> bool:
        """Whether it asks for this title."""
        if self.kind not in (None, kind):
            return False
        for mine, other in ((self.tmdb_id, tmdb_id), (self.tvdb_id, tvdb_id)):
            if mine and other:
                return mine == other
        return catalog.normalise_title(self.title) == catalog.normalise_title(title)


@dataclass(frozen=True, slots=True)
class _Notice:
    request: Request
    mine: bool
    named: bool  # it names the person's own account


def requests_of(conn: sqlite3.Connection, username: str) -> list[Request]:
    """What *username* asked for, newest first, a title as often as it was asked for.

    The audit log names who was signed in, the notification what was asked for. A request made
    from an emailed link has no account in either: it is that of the one account with the
    address it was sent to, unless the audit log says another account made it.
    """
    user = conn.execute(
        "SELECT id, username, email, created_at FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    if user is None:
        return []
    # rationale: the fixed actions only; every value is bound
    audits = conn.execute(
        "SELECT media_item_id, detail, created_at,"
        " actor_id = :id OR (actor_id IS NULL AND actor = :name"
        " AND julianday(created_at) >= julianday(:since)) AS mine FROM audit_log"
        " WHERE action IN ('downloaded', 're_downloaded') AND COALESCE(actor, '') != ''",
        {"id": user["id"], "name": user["username"], "since": user["created_at"]},
    ).fetchall()
    notices = _notices(conn, user)
    found: list[Request] = []
    taken: set[int] = set()
    for row in audits:
        at = parse_iso_utc(row["created_at"])
        if at is None:
            continue
        open_ones = {
            place
            for place, notice in enumerate(notices)
            if place not in taken and (row["mine"] or not notice.named)
        }
        own = _recorded_with(row["media_item_id"], row["detail"] or "", at, notices, open_ones)
        if own is not None:
            taken.add(own)
        if not row["mine"]:
            continue
        if own is not None:
            found.append(replace(notices[own].request, asked_at=at))
        elif (named := _named(row["media_item_id"], row["detail"] or "", at)) is not None:
            found.append(named)
    for place, notice in enumerate(notices):
        # One an audit row was written with is that row's person's.
        if place not in taken and notice.mine:
            found.append(notice.request)
    return sorted(found, key=lambda r: r.asked_at, reverse=True)


def _notices(conn: sqlite3.Connection, user: sqlite3.Row) -> list[_Notice]:
    rows = conn.execute(
        "SELECT title, media_type, tmdb_id, tvdb_id, season_number, notified, created_at,"
        " username, :email IS NOT NULL AND email = :email COLLATE NOCASE"
        " AND julianday(created_at) >= julianday(:since)"
        " AND NOT EXISTS (SELECT 1 FROM admin_users u WHERE u.email = :email COLLATE NOCASE"
        " AND u.id != :id) AS by_address"
        " FROM download_notifications WHERE title != '' AND email != ''"
        " AND (username IS NULL OR username = :name) ORDER BY id",
        {
            "email": user["email"] or None,
            "since": user["created_at"],
            "id": user["id"],
            "name": user["username"],
        },
    ).fetchall()
    found = []
    for r in rows:
        at = parse_iso_utc(r["created_at"])
        if at is None:
            continue
        request = Request(
            "movie" if r["media_type"] == "movie" else "tv",
            r["title"],
            r["tmdb_id"],
            r["tvdb_id"],
            r["season_number"],
            at,
            r["notified"] != 1,
        )
        mine = r["username"] is not None or bool(r["by_address"])
        found.append(_Notice(request, mine, r["username"] is not None))
    return found


def _added_to(name: str, detail: str) -> Kind | None:
    if detail.startswith(f"Added '{name}' to Radarr"):
        return "movie"
    return "tv" if detail.startswith(f"Added '{name}' to Sonarr") else None


def _recorded_with(
    name: str, detail: str, at: datetime, notices: Sequence[_Notice], places: Collection[int]
) -> int | None:
    """The place of the notification written with the audit row of *name*, or None."""
    id_of = _ID_RE.fullmatch(name)
    kind = _added_to(name, detail)
    for place, notice in enumerate(notices):
        asked = notice.request
        if place not in places or abs(asked.asked_at - at) > _TOGETHER:
            continue
        if id_of is None:
            same = asked.title == name and kind in (None, asked.kind)
        else:
            id_kind, number, _season = id_of.groups()
            same = int(number) == (asked.tmdb_id if id_kind == "tmdb" else asked.tvdb_id)
        if same:
            return place
    return None


def _named(name: str, detail: str, at: datetime) -> Request | None:
    """The request of an audit row with no notification: it knows an id or a title."""
    id_of = _ID_RE.fullmatch(name)
    if id_of is None:
        kind = _added_to(name, detail)
        return Request(kind, name, None, None, None, at, False) if name else None
    id_kind, number, season = id_of.groups()
    if id_kind == "tmdb":
        return Request("movie", "", int(number), None, None, at, False)
    return Request("tv", "", None, int(number), int(season) if season else None, at, False)


@dataclass(frozen=True, slots=True)
class Held:
    """The library, as requests are matched to it, and what the person has seen of it."""

    index: catalog.LibraryIndex
    movies: dict[str, lib.LibraryMovie]
    shows: dict[str, lib.LibraryShow]
    seen_films: set[str]
    seen_shows: dict[str, lib.ShowSeen]
    _by_title: dict[tuple[Kind, str], list[str]] = field(default_factory=dict)

    def refs_titled(self, kind: Kind | None, title: str) -> list[str]:
        """The refs of the titles of that name, of either kind when *kind* is None."""
        kinds: tuple[Kind, ...] = (kind,) if kind else ("movie", "tv")
        name = catalog.normalise_title(title)
        return [ref for k in kinds for ref in self._by_title.get((k, name), [])]

    def ref_of(self, request: Request) -> str | None:
        """The library ref of what was asked for. By its name alone only where one title has it."""
        if request.kind == "movie" and request.tmdb_id:
            return self.index.movies_by_tmdb.get(request.tmdb_id)
        if request.kind == "tv" and (request.tmdb_id or request.tvdb_id):
            by_tvdb = self.index.shows_by_tvdb.get(request.tvdb_id or 0)
            return by_tvdb or self.index.shows_by_tmdb.get(request.tmdb_id or 0)
        named = self.refs_titled(request.kind, request.title) if request.title else []
        return named[0] if len(named) == 1 else None

    def is_there(self, ref: str, season: int | None) -> bool:
        """Whether the library has the title, and its *season* if that was asked for. The index,
        held a while, may name one that is gone."""
        kind, _, key = ref.partition("-")
        if kind == "movie":
            return key in self.movies
        return key in self.shows and (season is None or season in self._seen(key).seasons)

    def is_watched(self, ref: str, season: int | None) -> bool:
        kind, _, key = ref.partition("-")
        if kind == "movie":
            return key in self.seen_films
        show = self._seen(key)
        if season is None:
            return show.watched
        return season in show.seasons and show.seasons[season].watched

    def _seen(self, key: str) -> lib.ShowSeen:
        return self.seen_shows.get(key) or lib.ShowSeen()


def held_library(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    index: catalog.LibraryIndex,
) -> Held:
    movies = {m.key: m for m in lib.library_movies(conn, libraries)}
    shows = {s.key: s for s in lib.library_shows(conn, libraries)}
    held = Held(
        index,
        movies,
        shows,
        seen_movies(conn, username) | _films_played_in_plex(conn, username),
        _with_plex_plays(conn, username, lib.show_seen(conn, username, libraries)),
    )
    for m in movies.values():
        held._by_title.setdefault(("movie", catalog.normalise_title(m.title)), []).append(
            f"movie-{m.key}"
        )
    for s in shows.values():
        held._by_title.setdefault(("tv", catalog.normalise_title(s.title)), []).append(
            f"show-{s.key}"
        )
    return held


def _films_played_in_plex(conn: sqlite3.Connection, username: str) -> set[str]:
    rows = conn.execute(
        "SELECT rating_key FROM plex_plays WHERE username = ? AND media_type = 'movie'"
        " AND rating_key IS NOT NULL",
        (username,),
    )
    return {r["rating_key"] for r in rows}


def _with_plex_plays(
    conn: sqlite3.Connection, username: str, seen: dict[str, lib.ShowSeen]
) -> dict[str, lib.ShowSeen]:
    """*seen* with the episodes played in Plex's own apps that it does not count."""
    rows = conn.execute(
        "SELECT mi.show_rating_key, mi.season_number, COUNT(*) AS played"
        " FROM media_items mi, json_each(mi.episode_keys) e"
        " WHERE mi.show_rating_key IN (SELECT show_rating_key FROM plex_plays"
        " WHERE username = :name) AND mi.season_number > 0"
        " AND EXISTS (SELECT 1 FROM plex_plays p WHERE p.username = :name"
        " AND p.rating_key = json_extract(e.value, '$.key'))"
        " AND NOT EXISTS (SELECT 1 FROM watch_progress wp WHERE wp.username = :name"
        " AND wp.rating_key = json_extract(e.value, '$.key')"
        " AND (wp.watched = 1 OR wp.seen_at IS NOT NULL))"
        " GROUP BY mi.show_rating_key, mi.season_number",
        {"name": username},
    ).fetchall()
    for r in rows:
        season = seen.get(r["show_rating_key"], lib.ShowSeen()).seasons.get(r["season_number"])
        if season is not None:
            seen[r["show_rating_key"]].seasons[r["season_number"]] = replace(
                season, seen=season.seen + r["played"]
            )
    return seen


def is_over(conn: sqlite3.Connection, username: str, requests: Iterable[Request]) -> set[Request]:
    """Those of *requests* whose title was deleted, or played by the person, since: for one the
    library does not hold, over and done with unless it was asked for again."""
    deleted = conn.execute(
        "SELECT d.media_type, d.tmdb_id, d.tvdb_id, d.season_number, d.title, a.created_at"
        " FROM deleted_media d JOIN audit_log a ON a.id = d.audit_id"
    ).fetchall()
    played = conn.execute(
        "SELECT media_type, tmdb_id, NULL AS tvdb_id, NULL AS season_number, title,"
        " played_at AS created_at FROM played_titles WHERE username = ? AND media_type = 'movie'",
        (username,),
    ).fetchall()
    ends: list[tuple[sqlite3.Row, datetime]] = [
        (row, at) for row in (*deleted, *played) if (at := parse_iso_utc(row["created_at"]))
    ]
    return {
        request
        for request in requests
        if any(
            at >= request.asked_at
            and (request.season is None or row["season_number"] in (None, request.season))
            and request.names(
                "movie" if row["media_type"] == "movie" else "tv",
                row["tmdb_id"],
                row["tvdb_id"],
                row["title"],
            )
            for row, at in ends
        )
    }


def has_any(
    conn: sqlite3.Connection, username: str, libraries: list[str], index: catalog.LibraryIndex
) -> bool:
    """Whether something they asked for is there unwatched, or is still waited for, as far as
    the database knows: Radarr and Sonarr are not asked."""
    asked = requests_of(conn, username)
    if not asked:
        return False
    held = held_library(conn, username, libraries, index)
    for request in asked:
        ref = held.ref_of(request)
        if ref is None or not held.is_there(ref, request.season):
            if request.waiting:
                return True
        elif not held.is_watched(ref, request.season):
            return True
    return False
