"""Read the Watch library from what the library sync stored in ``media_items``.

Only items whose Plex library is ticked in Settings (the ``plex_libraries``
setting the scanner reads) count; every query here takes that list and
filters on it, so nothing outside the scanned libraries can be listed,
searched or looked up.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import unicodedata
from collections.abc import Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from mediaman.core.removed_files import FILES_GONE
from mediaman.services.infra import get_setting
from mediaman.services.watch_catalog import folder_year
from mediaman.web.repository import seen_titles
from mediaman.web.repository.library_query import (
    ALL_SEASON_TYPES,
    ANIME_SEASON_TYPES,
    FORGIVEN,
    TitleSearch,
    without_article,
)
from mediaman.web.repository.watch_progress import seen_movies


@dataclass(frozen=True, slots=True)
class LibraryMovie:
    key: str
    title: str
    added_at: str
    tmdb_id: int | None = None
    year: int | None = None


@dataclass(frozen=True, slots=True)
class LibraryShow:
    key: str  # the show's Plex rating key (media_items.show_rating_key)
    title: str
    anime: bool
    season_count: int
    added_at: str
    tvdb_id: int | None = None
    year: int | None = None
    tmdb_id: int | None = None


@dataclass(frozen=True, slots=True)
class LibraryTitle:
    """One title of All titles: a movie or show Watch holds (*ref*), or one only a scan knows."""

    key: str  # "id:<media item>" or "show:<show>"
    title: str
    kind: Literal["movies", "tv", "anime"]
    added_at: str
    year: int | None = None
    ref: str | None = None
    size_bytes: int = 0
    watched_at: str | None = None  # by anyone
    leaving_at: str | None = None
    kept: bool = False
    stale_bytes: int | None = None  # what of it is stale; None: nothing is


type Orderable = LibraryMovie | LibraryShow | LibraryTitle

# The order of a library row: A to Z, newest added, newest released, or (A to Z) the titles a
# person has not seen before those they have.
RowOrder = Literal["az", "added", "year", "unwatched"]
# Those of All titles that go by what only an admin is shown: size either way, last watched by
# anyone either way, and the soonest to leave.
AdminOrder = Literal["size", "small", "seen", "stalest", "leaving"]
TitleOrder = RowOrder | AdminOrder

# Where a page of titles ended: what its last title is ordered by, its title folded for
# comparing, and its key.
RowCursor = tuple[str, str, str]

_HIGHEST_FIRST = ("added", "year", "size", "seen")


@dataclass(frozen=True, slots=True)
class SeasonSeen:
    """How many of a season's episodes a person has seen."""

    seen: int
    total: int | None  # None until a scan has counted the episodes
    marked: bool = False  # whole, with what a mark of the show covers, whatever is counted
    by_mark: frozenset[str] = frozenset()  # of the seen ones, those a mark alone covers

    @property
    def watched(self) -> bool:
        return self.marked or (bool(self.total) and self.seen >= (self.total or 0))


@dataclass(slots=True)
class ShowSeen:
    """A person's seen episodes of one show, by the number of each regular season."""

    seasons: dict[int, SeasonSeen] = field(default_factory=dict)

    @property
    def watched(self) -> bool:
        return bool(self.seasons) and all(s.watched for s in self.seasons.values())

    @property
    def left(self) -> int | None:
        """Episodes left of a show partly seen; None when none or all are seen, or a season's
        episodes are not counted yet."""
        if not any(s.seen or s.marked for s in self.seasons.values()):
            return None
        left = 0
        for s in self.seasons.values():
            if s.marked:
                continue
            if not s.total:
                return None
            left += s.total - min(s.seen, s.total)
        return left or None


@dataclass(frozen=True, slots=True)
class PendingDeletion:
    media_item_id: str
    action_id: int
    execute_at: str
    title: str
    season_number: int | None  # None for a movie
    show_key: str | None


# (action, execute_at) of a keep in force.
Keep = tuple[str, str | None]


@dataclass(frozen=True, slots=True)
class TitleFacts:
    """What the library holds for one movie, or for every synced season of one show."""

    title: str
    item_ids: list[str]
    size_bytes: int
    added_at: str
    last_watched_at: str | None
    item_keeps: dict[str, Keep]  # the strongest keep in force on each kept item
    show_keep: Keep | None  # a show keep in force


# Matches the storage panel's recently-deleted window (5 batches of 50).
_DELETED_POSTER_WINDOW = 250


def scanned_library_ids(conn: sqlite3.Connection) -> list[str]:
    """The Plex library ids ticked in Settings; [] when unset or unreadable."""
    value = get_setting(conn, "plex_libraries", default=[])
    return [str(v) for v in value] if isinstance(value, list) else []


def _in_libraries(
    libraries: list[str], column: str = "plex_library_id"
) -> tuple[str, list[object]]:
    if not libraries:
        return "0", []
    marks = ",".join("?" * len(libraries))
    return f"CAST({column} AS TEXT) IN ({marks})", list(libraries)


def _movies(
    conn: sqlite3.Connection,
    libraries: list[str],
    *,
    key: str | None = None,
    search: TitleSearch | None = None,
) -> list[LibraryMovie]:
    lib_sql, params = _in_libraries(libraries)
    where = [lib_sql, "media_type = 'movie'"]
    if key is not None:
        where.append("id = ?")
        params.append(key)
    # rationale: only fixed fragments and "?" placeholders are joined; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT id, title, added_at, tmdb_id, year, file_path FROM media_items WHERE {' AND '.join(where)}"
        " ORDER BY title COLLATE NOCASE, id",
        params,
    ).fetchall()
    return [
        LibraryMovie(
            r["id"],
            r["title"],
            r["added_at"],
            r["tmdb_id"],
            r["year"] or folder_year(r["file_path"]),
        )
        for r in rows
        if search is None or search.distance(r["title"]) is not None
    ]


def _shows(
    conn: sqlite3.Connection,
    libraries: list[str],
    *,
    key: str | None = None,
    search: TitleSearch | None = None,
) -> list[LibraryShow]:
    lib_sql, params = _in_libraries(libraries)
    season_marks = ",".join("?" * len(ALL_SEASON_TYPES))
    anime_marks = ",".join("?" * len(ANIME_SEASON_TYPES))
    where = [lib_sql, f"media_type IN ({season_marks})", "show_rating_key IS NOT NULL"]
    params.extend(ALL_SEASON_TYPES)
    if key is not None:
        where.append("show_rating_key = ?")
        params.append(key)
    # rationale: only fixed fragments and "?" placeholders are joined; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT s.*, COALESCE(s.tmdb_id, (SELECT m.tmdb_id FROM title_meta m"
        " WHERE m.id_type = 'tvdb' AND m.id = s.tvdb_id)) AS known_tmdb_id FROM ("
        "SELECT show_rating_key, MAX(COALESCE(show_title, title)) AS show_title,"
        f" MAX(media_type IN ({anime_marks})) AS anime,"
        " COUNT(DISTINCT CASE WHEN season_number > 0 THEN season_number END) AS seasons,"
        " MAX(added_at) AS added_at, MAX(tvdb_id) AS tvdb_id, NULLIF(MAX(tmdb_id), 0) AS tmdb_id,"
        " MAX(year) AS year, MIN(file_path) AS file_path"
        f" FROM media_items WHERE {' AND '.join(where)}"
        " GROUP BY show_rating_key) s ORDER BY s.show_title COLLATE NOCASE, s.show_rating_key",
        (*ANIME_SEASON_TYPES, *params),
    ).fetchall()
    return [
        LibraryShow(
            key=r["show_rating_key"],
            title=r["show_title"],
            anime=bool(r["anime"]),
            season_count=r["seasons"],
            added_at=r["added_at"],
            tvdb_id=r["tvdb_id"],
            # a title not scanned since years were stored has only its folder's
            year=r["year"] or folder_year(r["file_path"]),
            tmdb_id=r["known_tmdb_id"],
        )
        for r in rows
        if search is None or search.distance(r["show_title"]) is not None
    ]


def library_movies(conn: sqlite3.Connection, libraries: list[str]) -> list[LibraryMovie]:
    """Every movie in the scanned libraries, by title."""
    return _movies(conn, libraries)


def library_shows(conn: sqlite3.Connection, libraries: list[str]) -> list[LibraryShow]:
    """Every show (TV and anime) in the scanned libraries, grouped from its seasons, by title."""
    return _shows(conn, libraries)


def _plain(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return plain.casefold().strip()


def sort_title(title: str) -> str:
    """*title* as A to Z reads it: without a leading article, accents or case."""
    return without_article(_plain(title))


def row_cursor(
    item: Orderable,
    order: TitleOrder = "added",
    seen: Set[str] = frozenset(),
) -> RowCursor:
    """Where *item* stands in *order*; *seen* is seen_refs' answer, for Not watched first."""
    if order == "added":
        return item.added_at or "", item.title.casefold(), item.key
    name = sort_title(item.title)
    if order == "year":
        return f"{item.year:04d}" if item.year else "", name, item.key
    if order == "unwatched":
        return ("1" if _ref_of(item) in seen else "0") + name, item.title.casefold(), item.key
    if order != "az" and isinstance(item, LibraryTitle):
        return _admin_first(item, order), name, item.key
    return name, item.title.casefold(), item.key


def _ref_of(item: Orderable) -> str | None:
    if isinstance(item, LibraryTitle):
        return item.ref
    return f"{'movie' if isinstance(item, LibraryMovie) else 'show'}-{item.key}"


def _admin_first(item: LibraryTitle, order: AdminOrder) -> str:
    """What *item* is ordered by; those never watched are the longest unwatched, and what is not
    leaving goes last."""
    if order in ("size", "small"):
        return f"{item.size_bytes:020d}"
    if order in ("seen", "stalest"):
        return item.watched_at or ""
    return f"0{item.leaving_at}" if item.leaving_at else "1"


def cursor_text(cursor: RowCursor) -> str:
    """*cursor* as text safe in a URL; parse_cursor reads it back."""
    return base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode().rstrip("=")


def parse_cursor(text: str) -> RowCursor | None:
    """The cursor in *text*; None when it is not one of cursor_text's."""
    try:
        value = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (ValueError, RecursionError):
        return None
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(part, str) for part in value)
    ):
        return None
    return value[0], value[1], value[2]


def newest_first[T: Orderable](items: Sequence[T]) -> list[T]:
    """*items* newest added first, by the date they were added."""
    return in_order(items, "added")


def in_order[T: Orderable](
    items: Sequence[T], order: TitleOrder, seen: Set[str] = frozenset()
) -> list[T]:
    """*items* in *order*; ties by title, and an item with no date or year last."""
    at = {item.key: row_cursor(item, order, seen) for item in items}
    ordered = sorted(items, key=lambda item: at[item.key][1:])
    return sorted(ordered, key=lambda item: at[item.key][0], reverse=order in _HIGHEST_FIRST)


def row_page[T: Orderable](
    ordered: Sequence[T],
    after: RowCursor | None,
    limit: int,
    order: TitleOrder = "added",
    seen: Set[str] = frozenset(),
) -> tuple[list[T], RowCursor | None]:
    """The next *limit* of the items, which are in *order*, past *after*, and where they end (None
    at the end). Found by value, so a title that comes or goes meanwhile moves no other."""
    rest = [
        item
        for item in ordered
        if after is None or _is_past(row_cursor(item, order, seen), after, order)
    ]
    page = rest[:limit]
    return page, row_cursor(page[-1], order, seen) if len(rest) > limit else None


def _is_past(cursor: RowCursor, after: RowCursor, order: TitleOrder) -> bool:
    if order not in _HIGHEST_FIRST:
        return cursor > after
    return cursor[0] < after[0] or (cursor[0] == after[0] and cursor[1:] > after[1:])


def show_seen(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    *,
    show_key: str | None = None,
) -> dict[str, ShowSeen]:
    """*username*'s seen episodes of each show of the scanned libraries (or only of *show_key*),
    by show rating key, with those a mark of the show covers. Specials do not count."""
    lib_sql, params = _in_libraries(libraries, "mi.plex_library_id")
    season_marks = ",".join("?" * len(ALL_SEASON_TYPES))
    where = [
        lib_sql,
        f"mi.media_type IN ({season_marks})",
        "mi.show_rating_key IS NOT NULL",
        "mi.season_number > 0",
    ]
    params.extend(ALL_SEASON_TYPES)
    if show_key is not None:
        where.append("mi.show_rating_key = ?")
        params.append(show_key)
    seen_rows = "FROM watch_progress wp WHERE wp.username = ? AND (wp.watched = 1 OR wp.seen_at IS NOT NULL)"
    # rationale: only fixed fragments and "?" placeholders are joined; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT mi.id, mi.show_rating_key, mi.season_number, mi.episode_count,"
        # by its own episodes, whichever season their rows were saved under: a row of one no longer
        # in it (replaced under a new key, or removed) does not count; a season with no list yet
        # counts every row of its own
        " CASE WHEN mi.episode_keys IS NULL THEN"
        f" (SELECT COUNT(*) {seen_rows} AND wp.media_item_id = mi.id)"
        f" ELSE (SELECT COUNT(*) {seen_rows} AND wp.rating_key IN"
        " (SELECT json_extract(e.value, '$.key') FROM json_each(mi.episode_keys) e)) END AS seen"
        f" FROM media_items mi WHERE {' AND '.join(where)}",
        (username, username, *params),
    ).fetchall()
    covered = {
        c.season_id: c
        for c in seen_titles.covered_seasons(conn, username, libraries, show_key=show_key)
    }
    found: dict[str, ShowSeen] = {}
    for r in rows:
        seasons = found.setdefault(r["show_rating_key"], ShowSeen()).seasons
        season = covered.get(r["id"])
        by_mark = season.episodes if season else frozenset()
        seasons[r["season_number"]] = SeasonSeen(
            r["seen"] + len(by_mark),
            r["episode_count"],
            season is not None and season.whole,
            by_mark,
        )
    return found


def seen_films(conn: sqlite3.Connection, username: str, libraries: list[str]) -> set[str]:
    """The rating keys of the films *username* has seen."""
    return seen_movies(conn, username) | seen_titles.seen_films(conn, username, libraries)


def seen_refs(conn: sqlite3.Connection, username: str, libraries: list[str]) -> set[str]:
    """The refs ("movie-<key>", "show-<key>") of the library titles *username* has seen."""
    shows = show_seen(conn, username, libraries)
    return {f"movie-{key}" for key in seen_films(conn, username, libraries)} | {
        f"show-{key}" for key, seen in shows.items() if seen.watched
    }


def mark_fields(ref: str | None, seen: set[str]) -> dict[str, object]:
    """The Watched fields of a card or title: *ref* is its library ref, None when the library
    does not hold it, and *seen* is seen_refs' answer."""
    if ref is None:
        return {"watched": False, "mark_kind": None, "mark_key": None}
    kind, _, key = ref.partition("-")
    return {"watched": ref in seen, "mark_kind": kind, "mark_key": key}


def find_movie(conn: sqlite3.Connection, libraries: list[str], key: str) -> LibraryMovie | None:
    found = _movies(conn, libraries, key=key)
    return found[0] if found else None


def find_show(conn: sqlite3.Connection, libraries: list[str], key: str) -> LibraryShow | None:
    found = _shows(conn, libraries, key=key)
    return found[0] if found else None


# Of the titles that hold what was typed, so few that the forgiving search is made too.
_FEW = 3
_YEAR = re.compile(r"(?:19|20)\d\d")


def _contain(
    conn: sqlite3.Connection, libraries: list[str], query: str
) -> tuple[list[LibraryMovie | LibraryShow], TitleSearch]:
    search = TitleSearch(conn, query)
    found: list[LibraryMovie | LibraryShow] = [
        *_movies(conn, libraries, search=search),
        *_shows(conn, libraries, search=search),
    ]
    if len(found) < _FEW:
        search = TitleSearch(conn, query, forgiving=True)
        found = [*_movies(conn, libraries, search=search), *_shows(conn, libraries, search=search)]
    return found, search


def search_library(
    conn: sqlite3.Connection, libraries: list[str], query: str, limit: int
) -> tuple[list[LibraryMovie | LibraryShow], int]:
    """The *limit* movies and shows whose titles come closest to *query* of those that contain
    it (as TitleSearch reads both), then by title, and how many contain it. Where few contain
    it, those that differ from it by a plural or a small typo, or hold its words in another
    order, are found too, after them. A year typed last is also left off the words looked for,
    and the titles of that year come first."""
    words = query.split()
    year = int(words[-1]) if len(words) > 1 and _YEAR.fullmatch(words[-1]) else None
    searches = [_contain(conn, libraries, query)]
    if year is not None:
        searches.append(_contain(conn, libraries, " ".join(words[:-1])))
    found: dict[tuple[type, str], LibraryMovie | LibraryShow] = {}
    for items, _ in searches:
        for item in items:
            found.setdefault((type(item), item.key), item)

    def rank(item: LibraryMovie | LibraryShow) -> tuple[bool, int, str, str]:
        near = [d for _, search in searches if (d := search.distance(item.title)) is not None]
        return (
            year is not None and item.year != year,
            min(near, default=FORGIVEN),
            sort_title(item.title),
            item.key,
        )

    ranked = sorted(found.values(), key=rank)
    return ranked[:limit], len(ranked)


def season_item_id(
    conn: sqlite3.Connection, libraries: list[str], show_key: str, season_number: int
) -> str | None:
    """The ``media_items.id`` of one season of a show, if that season was synced."""
    lib_sql, params = _in_libraries(libraries)
    # rationale: only the fixed library filter and "?" placeholders are interpolated
    row = conn.execute(  # nosec B608
        f"SELECT id FROM media_items WHERE {lib_sql}"
        " AND show_rating_key = ? AND season_number = ? ORDER BY id LIMIT 1",
        (*params, show_key, season_number),
    ).fetchone()
    return row["id"] if row else None


def pending_deletions(
    conn: sqlite3.Connection,
    libraries: list[str],
    *,
    media_item_id: str | None = None,
) -> list[PendingDeletion]:
    """Movies and seasons scheduled for deletion that are neither kept, begun by the deletion
    pass nor already removed, soonest first. One whose date has passed is leaving until the pass claims it."""
    lib_sql, params = _in_libraries(libraries, "mi.plex_library_id")
    where = [
        lib_sql,
        "sa.action = 'scheduled_deletion'",
        "sa.token_used = 0",
        "(sa.delete_status IS NULL OR sa.delete_status = 'pending')",
        "sa.execute_at IS NOT NULL",
        f"NOT {FILES_GONE}",
    ]
    if media_item_id is not None:
        where.append("sa.media_item_id = ?")
        params.append(media_item_id)
    # rationale: only fixed fragments and "?" placeholders are joined; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT sa.id AS action_id, sa.media_item_id, sa.execute_at, mi.title, mi.media_type,"
        " mi.show_title, mi.season_number, mi.show_rating_key"
        " FROM scheduled_actions sa JOIN media_items mi ON mi.id = sa.media_item_id"
        f" WHERE {' AND '.join(where)} ORDER BY sa.execute_at, sa.id",
        params,
    ).fetchall()
    result: list[PendingDeletion] = []
    for r in rows:
        is_movie = r["media_type"] == "movie"
        result.append(
            PendingDeletion(
                media_item_id=r["media_item_id"],
                action_id=r["action_id"],
                execute_at=r["execute_at"],
                title=r["title"] if is_movie else (r["show_title"] or r["title"]),
                season_number=None if is_movie else r["season_number"],
                show_key=None if is_movie else r["show_rating_key"],
            )
        )
    return result


def pending_deletion(
    conn: sqlite3.Connection, libraries: list[str], media_item_id: str
) -> PendingDeletion | None:
    found = pending_deletions(conn, libraries, media_item_id=media_item_id)
    return found[0] if found else None


def _stronger(a: Keep, b: Keep) -> Keep:
    if a[0] == "protected_forever" or b[0] == "protected_forever":
        return a if a[0] == "protected_forever" else b
    return a if (a[1] or "") >= (b[1] or "") else b


def title_facts(
    conn: sqlite3.Connection, libraries: list[str], kind: str, key: str, now: datetime
) -> TitleFacts | None:
    """Size, dates and keeps of a library movie (*kind* "movie") or show ("show"); None
    when it is not in the scanned libraries."""
    lib_sql, params = _in_libraries(libraries)
    if kind == "movie":
        where = "media_type = 'movie' AND id = ?"
    else:
        where = f"media_type IN ({','.join('?' * len(ALL_SEASON_TYPES))}) AND show_rating_key = ?"
        params.extend(ALL_SEASON_TYPES)
    params.append(key)
    # rationale: only fixed fragments and "?" placeholders are joined; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT id, COALESCE(show_title, title) AS title, added_at, file_size_bytes,"
        f" last_watched_at FROM media_items WHERE {lib_sql} AND {where} ORDER BY id",
        params,
    ).fetchall()
    if not rows:
        return None
    ids = [r["id"] for r in rows]
    now_iso = now.isoformat()
    marks = ",".join("?" * len(ids))
    item_keeps: dict[str, Keep] = {}
    # rationale: marks is purely "?" * len(ids); every value is bound
    for sa in conn.execute(  # nosec B608
        "SELECT media_item_id, action, execute_at FROM scheduled_actions"
        f" WHERE media_item_id IN ({marks}) AND (action = 'protected_forever'"
        " OR (action = 'snoozed' AND execute_at IS NOT NULL AND execute_at >= ?))",
        (*ids, now_iso),
    ).fetchall():
        keep: Keep = (sa["action"], sa["execute_at"])
        prev = item_keeps.get(sa["media_item_id"])
        item_keeps[sa["media_item_id"]] = keep if prev is None else _stronger(prev, keep)
    show_keep: Keep | None = None
    if kind == "show":
        ks = conn.execute(
            "SELECT action, execute_at FROM kept_shows WHERE show_rating_key = ?"
            " AND (action = 'protected_forever' OR (execute_at IS NOT NULL AND execute_at > ?))",
            (key, now_iso),
        ).fetchone()
        show_keep = (ks["action"], ks["execute_at"]) if ks else None
    watched = [r["last_watched_at"] for r in rows if r["last_watched_at"]]
    return TitleFacts(
        title=rows[0]["title"],
        item_ids=ids,
        size_bytes=sum(r["file_size_bytes"] or 0 for r in rows),
        added_at=max(r["added_at"] for r in rows),
        last_watched_at=max(watched) if watched else None,
        item_keeps=item_keeps,
        show_keep=show_keep,
    )


def is_poster_key_allowed(conn: sqlite3.Connection, libraries: list[str], key: str) -> bool:
    """Whether a poster may be served for *key*, to an admin or a viewer alike.

    True for the key of a movie or season in the scanned libraries, or of
    the show such a season belongs to, and for an item the audit log records
    as deleted (the storage panel still shows those, from the poster cache).
    """
    lib_sql, params = _in_libraries(libraries)
    # rationale: only the fixed library filter and "?" placeholders are interpolated
    row = conn.execute(  # nosec B608
        f"SELECT 1 FROM media_items WHERE {lib_sql}"
        " AND (id = ? OR plex_rating_key = ? OR show_rating_key = ?) LIMIT 1",
        (*params, key, key, key),
    ).fetchone()
    if row is not None:
        return True
    # The audit row keeps the rating key only inside its detail text, so look at the newest
    # deleted rows (the window the storage panel shows) rather than every audit row.
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE action = 'deleted' AND media_item_id = ? LIMIT 1",
        (key,),
    ).fetchone()
    if row is not None:
        return True
    row = conn.execute(
        "SELECT 1 FROM (SELECT detail FROM audit_log WHERE action = 'deleted'"
        " ORDER BY created_at DESC LIMIT ?) WHERE instr(detail, ?) > 0 LIMIT 1",
        (_DELETED_POSTER_WINDOW, f"[rk:{key}]"),
    ).fetchone()
    return row is not None


__all__ = [
    "AdminOrder",
    "LibraryMovie",
    "LibraryShow",
    "LibraryTitle",
    "PendingDeletion",
    "RowCursor",
    "RowOrder",
    "SeasonSeen",
    "ShowSeen",
    "TitleFacts",
    "TitleOrder",
    "cursor_text",
    "find_movie",
    "find_show",
    "in_order",
    "is_poster_key_allowed",
    "library_movies",
    "library_shows",
    "mark_fields",
    "newest_first",
    "parse_cursor",
    "pending_deletion",
    "pending_deletions",
    "row_cursor",
    "row_page",
    "scanned_library_ids",
    "search_library",
    "season_item_id",
    "seen_films",
    "seen_refs",
    "show_seen",
    "title_facts",
]
