"""Read the Watch library from what the library sync stored in ``media_items``.

Only items whose Plex library is ticked in Settings (the ``plex_libraries``
setting the scanner reads) count; every query here takes that list and
filters on it, so nothing outside the scanned libraries can be listed,
searched or looked up.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from mediaman.services.infra import get_setting
from mediaman.web.repository.library_query import (
    ALL_SEASON_TYPES,
    ANIME_SEASON_TYPES,
    MAX_SEARCH_TERM_LEN,
)
from mediaman.web.repository.watch_progress import seen_movies


@dataclass(frozen=True, slots=True)
class LibraryMovie:
    key: str
    title: str
    added_at: str
    tmdb_id: int | None = None


@dataclass(frozen=True, slots=True)
class LibraryShow:
    key: str  # the show's Plex rating key (media_items.show_rating_key)
    title: str
    anime: bool
    season_count: int
    added_at: str
    tvdb_id: int | None = None


# Where a newest-first page of titles ended: the last title's added date, its title folded
# for comparing, and its key.
RowCursor = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class SeasonSeen:
    """How many of a season's episodes a person has seen."""

    seen: int
    total: int | None  # None until a scan has counted the episodes

    @property
    def watched(self) -> bool:
        return bool(self.total) and self.seen >= (self.total or 0)


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
        if not any(s.seen for s in self.seasons.values()):
            return None
        left = 0
        for s in self.seasons.values():
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


# Matches the Dashboard's recently-deleted window (5 batches of 50).
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


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _movies(
    conn: sqlite3.Connection,
    libraries: list[str],
    *,
    key: str | None = None,
    term: str | None = None,
    limit: int = -1,
) -> list[LibraryMovie]:
    lib_sql, params = _in_libraries(libraries)
    where = [lib_sql, "media_type = 'movie'"]
    if key is not None:
        where.append("id = ?")
        params.append(key)
    if term is not None:
        where.append("title LIKE ? ESCAPE '\\'")
        params.append(_like(term))
    # rationale: only fixed fragments and "?" placeholders are joined; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT id, title, added_at, tmdb_id FROM media_items WHERE {' AND '.join(where)}"
        " ORDER BY title COLLATE NOCASE, id LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [LibraryMovie(r["id"], r["title"], r["added_at"], r["tmdb_id"]) for r in rows]


def _shows(
    conn: sqlite3.Connection,
    libraries: list[str],
    *,
    key: str | None = None,
    term: str | None = None,
    limit: int = -1,
) -> list[LibraryShow]:
    lib_sql, params = _in_libraries(libraries)
    season_marks = ",".join("?" * len(ALL_SEASON_TYPES))
    anime_marks = ",".join("?" * len(ANIME_SEASON_TYPES))
    where = [lib_sql, f"media_type IN ({season_marks})", "show_rating_key IS NOT NULL"]
    params.extend(ALL_SEASON_TYPES)
    if key is not None:
        where.append("show_rating_key = ?")
        params.append(key)
    if term is not None:
        where.append("COALESCE(show_title, title) LIKE ? ESCAPE '\\'")
        params.append(_like(term))
    # rationale: only fixed fragments and "?" placeholders are joined; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT show_rating_key, MAX(COALESCE(show_title, title)) AS show_title,"
        f" MAX(media_type IN ({anime_marks})) AS anime,"
        " COUNT(DISTINCT CASE WHEN season_number > 0 THEN season_number END) AS seasons,"
        " MAX(added_at) AS added_at, MAX(tvdb_id) AS tvdb_id"
        f" FROM media_items WHERE {' AND '.join(where)}"
        " GROUP BY show_rating_key ORDER BY show_title COLLATE NOCASE, show_rating_key LIMIT ?",
        (*ANIME_SEASON_TYPES, *params, limit),
    ).fetchall()
    return [
        LibraryShow(
            key=r["show_rating_key"],
            title=r["show_title"],
            anime=bool(r["anime"]),
            season_count=r["seasons"],
            added_at=r["added_at"],
            tvdb_id=r["tvdb_id"],
        )
        for r in rows
    ]


def library_movies(conn: sqlite3.Connection, libraries: list[str]) -> list[LibraryMovie]:
    """Every movie in the scanned libraries, by title."""
    return _movies(conn, libraries)


def library_shows(conn: sqlite3.Connection, libraries: list[str]) -> list[LibraryShow]:
    """Every show (TV and anime) in the scanned libraries, grouped from its seasons, by title."""
    return _shows(conn, libraries)


def row_cursor(item: LibraryMovie | LibraryShow) -> RowCursor:
    return item.added_at or "", item.title.casefold(), item.key


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


def newest_first[T: (LibraryMovie, LibraryShow)](items: Sequence[T]) -> list[T]:
    """*items* newest added first, by the date List's Recently added sort compares; ties by
    title, and an item with no date last."""
    by_title = sorted(items, key=lambda item: row_cursor(item)[1:])
    return sorted(by_title, key=lambda item: row_cursor(item)[0], reverse=True)


def row_page[T: (LibraryMovie, LibraryShow)](
    newest: Sequence[T], after: RowCursor | None, limit: int
) -> tuple[list[T], RowCursor | None]:
    """The next *limit* of the *newest*-first items past *after*, and where they end (None at
    the end). Found by value, so a title that comes or goes meanwhile moves no other."""
    rest = [item for item in newest if after is None or _is_past(row_cursor(item), after)]
    page = rest[:limit]
    return page, row_cursor(page[-1]) if len(rest) > limit else None


def _is_past(cursor: RowCursor, after: RowCursor) -> bool:
    return cursor[0] < after[0] or (cursor[0] == after[0] and cursor[1:] > after[1:])


def show_seen(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    *,
    show_key: str | None = None,
) -> dict[str, ShowSeen]:
    """*username*'s seen episodes of each show of the scanned libraries (or only of *show_key*),
    by show rating key. Specials do not count."""
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
    # rationale: only fixed fragments and "?" placeholders are joined; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT mi.show_rating_key, mi.season_number, mi.episode_count, COUNT(wp.rating_key) AS seen"
        " FROM media_items mi LEFT JOIN watch_progress wp ON wp.media_item_id = mi.id"
        " AND wp.username = ? AND (wp.watched = 1 OR wp.seen_at IS NOT NULL)"
        f" WHERE {' AND '.join(where)} GROUP BY mi.id",
        (username, *params),
    ).fetchall()
    found: dict[str, ShowSeen] = {}
    for r in rows:
        seasons = found.setdefault(r["show_rating_key"], ShowSeen()).seasons
        seasons[r["season_number"]] = SeasonSeen(r["seen"], r["episode_count"])
    return found


def seen_refs(conn: sqlite3.Connection, username: str, libraries: list[str]) -> set[str]:
    """The refs ("movie-<key>", "show-<key>") of the library titles *username* has seen."""
    shows = show_seen(conn, username, libraries)
    return {f"movie-{key}" for key in seen_movies(conn, username)} | {
        f"show-{key}" for key, seen in shows.items() if seen.watched
    }


def find_movie(conn: sqlite3.Connection, libraries: list[str], key: str) -> LibraryMovie | None:
    found = _movies(conn, libraries, key=key)
    return found[0] if found else None


def find_show(conn: sqlite3.Connection, libraries: list[str], key: str) -> LibraryShow | None:
    found = _shows(conn, libraries, key=key)
    return found[0] if found else None


def search_library(
    conn: sqlite3.Connection, libraries: list[str], query: str, limit: int
) -> list[LibraryMovie | LibraryShow]:
    """Movies and shows whose title contains *query* (case-insensitive), by title, at most *limit*."""
    term = query[:MAX_SEARCH_TERM_LEN]
    found: list[LibraryMovie | LibraryShow] = [
        *_movies(conn, libraries, term=term, limit=limit),
        *_shows(conn, libraries, term=term, limit=limit),
    ]
    found.sort(key=lambda item: item.title.casefold())
    return found[:limit]


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
    now: datetime,
    *,
    media_item_id: str | None = None,
) -> list[PendingDeletion]:
    """Movies and seasons scheduled for deletion and not yet kept or due, soonest first."""
    lib_sql, params = _in_libraries(libraries, "mi.plex_library_id")
    where = [
        lib_sql,
        "sa.action = 'scheduled_deletion'",
        "sa.token_used = 0",
        "(sa.delete_status IS NULL OR sa.delete_status = 'pending')",
        "sa.execute_at >= ?",
    ]
    params.append(now.isoformat())
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
    conn: sqlite3.Connection, libraries: list[str], media_item_id: str, now: datetime
) -> PendingDeletion | None:
    found = pending_deletions(conn, libraries, now, media_item_id=media_item_id)
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
    as deleted (the Dashboard still shows those, from the poster cache).
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
    # deleted rows (the window the Dashboard shows) rather than every audit row.
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
    "LibraryMovie",
    "LibraryShow",
    "PendingDeletion",
    "RowCursor",
    "SeasonSeen",
    "ShowSeen",
    "TitleFacts",
    "cursor_text",
    "find_movie",
    "find_show",
    "is_poster_key_allowed",
    "library_movies",
    "library_shows",
    "newest_first",
    "parse_cursor",
    "pending_deletion",
    "pending_deletions",
    "row_cursor",
    "row_page",
    "scanned_library_ids",
    "search_library",
    "season_item_id",
    "seen_refs",
    "show_seen",
    "title_facts",
]
