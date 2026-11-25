"""Reads for the History page: what was recorded and what was played, as one ordered list.

Every read takes a Viewer. An admin sees every row; anyone else only what their own account
did, which the SQL itself holds them to.
"""

from __future__ import annotations

import base64
import heapq
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from mediaman.core.audit import CLI_UNLOCK, UNSIGNED_ACTIONS
from mediaman.scanner.repository.audit import _FILTER_MAP, SHOW_ACTIONS, AuditRow

PLAYS = "plays"
PER_PAGE_MAX = 100
#: What a filter or a search is cut to.
TEXT_MAX = 200
_CURSOR_MAX = 300
_ID_MAX = 2**63 - 1
#: Plays of one title further apart than this are two sittings.
_SITTING_DAYS = 4 / 24

# At the same moment an event is listed before a play.
_EVENT, _PLAY = 1, 0


class BadCursor(ValueError):
    """The cursor is not one this module made."""


@dataclass(frozen=True, slots=True)
class Viewer:
    user_id: int
    username: str
    created_at: str
    is_admin: bool


@dataclass(frozen=True, slots=True)
class Filters:
    """*since* is inclusive and *until* exclusive; *action* is a filter name or "plays"."""

    q: str = ""
    user: str = ""
    action: str = ""
    title: str = ""
    since: datetime | None = None
    until: datetime | None = None


@dataclass(frozen=True, slots=True)
class PlayPart:
    """One film or episode of a sitting. The numbers and the name are None when not known."""

    rating_key: str | None
    season_number: int | None
    episode_number: int | None
    title: str | None
    finished: bool


@dataclass(frozen=True, slots=True)
class Play:
    """One person's sitting with one film or show."""

    ref: str
    username: str
    media_type: Literal["movie", "tv"]
    title: str
    #: The film's or the show's key in Plex.
    title_key: str | None
    started_at: str
    created_at: str
    finished: bool
    parts: tuple[PlayPart, ...]


Entry = AuditRow | Play


@dataclass(frozen=True, slots=True)
class Page:
    entries: list[Entry]
    #: Where the next page starts; None when there is none.
    next_cursor: str | None
    #: How many rows the filters match; None on every page but the first.
    total: int | None


def viewer_for(conn: sqlite3.Connection, username: str) -> Viewer | None:
    row = conn.execute(
        "SELECT id, username, created_at, role FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        return None
    return Viewer(row["id"], row["username"], row["created_at"], row["role"] == "admin")


def people(conn: sqlite3.Connection, viewer: Viewer) -> list[str]:
    """The names *viewer* may filter by: everyone's for an admin, else only their own."""
    if not viewer.is_admin:
        return [viewer.username]
    rows = conn.execute("SELECT username FROM admin_users ORDER BY username COLLATE NOCASE")
    return [r["username"] for r in rows]


def parse_filters(
    *,
    q: str = "",
    user: str = "",
    action: str = "",
    title: str = "",
    since: str = "",
    until: str = "",
) -> Filters:
    """Filters from an address's text. A day as *until* is included; what is no date is ignored."""
    return Filters(
        q=_text(q),
        user=_text(user),
        action=_text(action),
        title=_text(title),
        since=_moment(since),
        until=_moment(until, end=True),
    )


def _text(value: str) -> str:
    return value.replace("\0", "").encode(errors="ignore").decode().strip()[:TEXT_MAX]


def _moment(value: str, *, end: bool = False) -> datetime | None:
    try:
        if len(value) == 10:
            day = date.fromisoformat(value) + timedelta(days=int(end))
            return datetime(day.year, day.month, day.day, tzinfo=UTC)
        at = datetime.fromisoformat(value)
        return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


# --- cursor ---


@dataclass(frozen=True, slots=True)
class _Cursor:
    at: str
    rank: int
    ref: int | str


def _encode(at: str, rank: int, ref: int | str) -> str:
    raw = json.dumps([at, rank, ref], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str) -> _Cursor:
    if len(cursor) > _CURSOR_MAX:
        raise BadCursor
    try:
        at, rank, ref = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, TypeError, RecursionError):
        raise BadCursor from None
    event = type(ref) is int and rank == _EVENT and 0 <= ref <= _ID_MAX
    play = isinstance(ref, str) and rank == _PLAY
    if not isinstance(at, str) or type(rank) is not int or not (event or play):
        raise BadCursor
    try:
        f"{at}{ref}".encode()
    except UnicodeEncodeError:
        raise BadCursor from None
    return _Cursor(at, rank, ref)


def cursor_of(event: AuditRow) -> str:
    """The cursor of the page that starts with *event*."""
    return _encode(event.created_at, _EVENT, event.id + 1)


def _order(entry: Entry) -> tuple[str, int, int | str]:
    if isinstance(entry, Play):
        return entry.created_at, _PLAY, entry.ref
    return entry.created_at, _EVENT, entry.id


# --- SQL conditions ---


class _Where:
    """Conditions joined by AND, with what they bind."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.params: list[object] = []

    def add(self, sql: str, *params: object) -> None:
        self.parts.append(sql)
        self.params += params

    def __str__(self) -> str:
        return " AND ".join(self.parts) or "1"


def _like(text: str) -> str:
    for char in "\\%_":
        text = text.replace(char, "\\" + char)
    return f"%{text}%"


def _any_like(columns: Sequence[str]) -> str:
    return "(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns) + ")"


def _dates(where: _Where, column: str, filters: Filters) -> None:
    if filters.since:
        where.add(f"{column} >= ?", filters.since.isoformat())
    if filters.until:
        where.add(f"{column} < ?", filters.until.isoformat())


def _add_text(
    where: _Where, filters: Filters, titles: Sequence[str], names: Sequence[str], only: str = ""
) -> None:
    """*names* are what the search reads beside the title; *only* goes with every title match."""
    for text, columns in ((filters.title, ()), (filters.q, names)):
        if not text:
            continue
        pattern = _like(text)
        sql = only + _any_like(titles)
        if columns:
            sql = f"(({sql}) OR {_any_like(columns)})"
        where.add(sql, *[pattern] * (len(titles) + len(columns)))


_SHOW_ACTIONS_SQL = ",".join(f"'{a}'" for a in SHOW_ACTIONS)

_JOINS = f"""
LEFT JOIN media_items mi
  ON mi.id = al.media_item_id AND al.action NOT IN ({_SHOW_ACTIONS_SQL})
    AND al.media_item_id != '_security'
LEFT JOIN kept_shows ks
  ON ks.show_rating_key = al.media_item_id AND al.action IN ({_SHOW_ACTIONS_SQL})
"""

_EVENT_COLUMNS = """
al.id, al.media_item_id, al.action, al.detail, al.space_reclaimed_bytes, al.created_at, al.actor,
mi.title AS mi_title, mi.plex_rating_key, ks.show_title AS ks_title
"""


def _scopes(viewer: Viewer) -> list[_Where]:
    """The events *viewer* may see, as conditions that never match the same row."""
    if viewer.is_admin:
        return [_Where()]
    own = _Where()
    own.add("al.actor_id = ?", viewer.user_id)
    # Rows without an account's id: theirs by name, but not a person's before them of that name.
    # The plus keeps the read on the index of names, not on every row without an id.
    named = _Where()
    named.add("+al.actor_id IS NULL AND al.actor = ?", viewer.username)
    named.add("julianday(al.created_at) >= julianday(?)", viewer.created_at)
    named.add(f"al.action NOT IN ({','.join('?' * len(UNSIGNED_ACTIONS))})", *UNSIGNED_ACTIONS)
    named.add("NOT (al.action = ? AND al.actor = ?)", *CLI_UNLOCK)
    return [own, named]


def _event_filters(where: _Where, viewer: Viewer, filters: Filters) -> None:
    if filters.user:
        where.add("al.actor = ?", filters.user)
    if filters.action == "security":
        where.add("al.action >= 'sec:' AND al.action < 'sec;'")
    elif filters.action:
        actions = _FILTER_MAP.get(filters.action, (filters.action,))
        where.add(f"al.action IN ({','.join('?' * len(actions))})", *actions)
    titles = ["mi.title", "mi.show_title", "ks.show_title", "al.media_item_id"]
    if viewer.is_admin:
        # A removed title is named only in the text, which can also hold what is not a person's
        # own to find by.
        titles.append("al.detail")
    _add_text(
        where,
        filters,
        titles,
        ["al.actor"] if viewer.is_admin else [],
        only="al.media_item_id != '_security' AND ",
    )
    _dates(where, "al.created_at", filters)


def _event_sources(viewer: Viewer, filters: Filters) -> list[_Where]:
    if filters.action == PLAYS:
        return []
    scopes = _scopes(viewer)
    for where in scopes:
        _event_filters(where, viewer, filters)
    return scopes


def _audit_row(r: sqlite3.Row) -> AuditRow:
    return AuditRow(**{k: r[k] for k in r.keys()})  # noqa: SIM118 - a Row iterates its values


def _read_events(
    conn: sqlite3.Connection, where: _Where, cursor: _Cursor | None, limit: int
) -> list[AuditRow]:
    if cursor is not None and cursor.rank == _EVENT:
        where.add("(al.created_at, al.id) < (?, ?)", cursor.at, cursor.ref)
    elif cursor is not None:
        where.add("al.created_at < ?", cursor.at)
    # rationale: *where* is made of fixed fragments; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT {_EVENT_COLUMNS} FROM audit_log al {_JOINS} WHERE {where}"
        " ORDER BY al.created_at DESC, al.id DESC LIMIT ?",
        (*where.params, limit),
    )
    return [_audit_row(r) for r in rows]


def event_for(conn: sqlite3.Connection, viewer: Viewer, event_id: int) -> AuditRow | None:
    """Event *event_id*, or None when there is none or *viewer* may not see it."""
    if not 0 < event_id <= _ID_MAX:
        return None
    for where in _scopes(viewer):
        where.add("al.id = ?", event_id)
        found = _read_events(conn, where, None, 1)
        if found:
            return found[0]
    return None


# --- plays ---

# Each branch holds one place plays are stored. Hand marks are not plays: a row that only holds
# one has the epoch as its time, and one made after the last play left its time later than that
# play's. played_titles adds the titles whose own rows are gone.
_PLAYS_SQL = """
WITH raw AS (
  SELECT wp.username, CASE WHEN wp.show_rating_key IS NULL THEN 'movie' ELSE 'tv' END AS media_type,
         CASE WHEN wp.show_rating_key IS NULL THEN mi.title
              ELSE COALESCE(mi.show_title, (SELECT MAX(s.show_title) FROM media_items s
                                            WHERE s.show_rating_key = wp.show_rating_key), mi.title)
         END AS title,
         COALESCE(wp.show_rating_key, wp.rating_key) AS title_key, wp.updated_at AS at,
         'w:' || wp.rowid AS ref, wp.rating_key, mi.id AS season_id, mi.season_number,
         NULL AS episode_number, NULL AS episode_title,
         wp.watched = 1 AND (wp.seen_at IS NULL OR wp.seen_at <= wp.updated_at) AS finished
  FROM watch_progress wp
  JOIN admin_users u ON u.username = wp.username
  LEFT JOIN media_items mi ON mi.id = wp.media_item_id
  WHERE wp.updated_at > '1971' AND {wp_user}
    AND (wp.position_s > 0 OR wp.watched = 1 OR wp.credited_at IS NOT NULL)
  UNION ALL
  SELECT p.username, CASE p.media_type WHEN 'movie' THEN 'movie' ELSE 'tv' END,
         CASE p.media_type WHEN 'movie' THEN p.title ELSE p.show_title END,
         CASE p.media_type WHEN 'movie' THEN p.rating_key ELSE p.show_rating_key END,
         p.viewed_at, 'x:' || p.rowid, p.rating_key, NULL, p.season_number, p.episode_number,
         CASE p.media_type WHEN 'movie' THEN NULL ELSE p.title END, 1
  FROM plex_plays p
  JOIN admin_users u ON u.username = p.username
  JOIN plex_links l ON l.username = p.username
  WHERE p.media_type IN ('movie', 'episode') AND {p_user}
  UNION ALL
  SELECT t.username, t.media_type, t.title, NULL, t.played_at, 't:' || t.rowid, NULL, NULL, NULL,
         NULL, NULL, 0
  FROM played_titles t
  JOIN admin_users u ON u.username = t.username
  WHERE {t_user}
),
marked AS (
  SELECT *, julianday(at) - julianday(LAG(at) OVER one_title) > {gap} AS starts
  FROM raw WHERE TRIM(title) != ''
  WINDOW one_title AS (PARTITION BY username, media_type, LOWER(title) ORDER BY at, ref)
),
numbered AS (
  SELECT *, SUM(COALESCE(starts, 0)) OVER
    (PARTITION BY username, media_type, LOWER(title) ORDER BY at, ref) AS sitting
  FROM marked
),
-- The bare columns are those of the row with the greatest moment.
sittings AS (
  SELECT username, media_type, title, at, ref, MAX(at || ' ' || ref),
         json_group_array(json_array(at, ref, title_key, rating_key, season_id, season_number,
                                     episode_number, episode_title, finished)) AS parts
  FROM numbered GROUP BY username, media_type, LOWER(title), sitting
)
"""


def _plays_query(viewer: Viewer, filters: Filters) -> tuple[str, dict[str, object], _Where] | None:
    if filters.action not in ("", PLAYS):
        return None
    names = {viewer.username} if not viewer.is_admin else set()
    if filters.user:
        if names and filters.user not in names:
            return None
        names = {filters.user}
    user = next(iter(names), None)
    scoped = "{0}.username = :user" if user is not None else "1"
    sql = _PLAYS_SQL.format(
        wp_user=scoped.format("wp"),
        p_user=scoped.format("p"),
        t_user=scoped.format("t"),
        gap=_SITTING_DAYS,
    )
    where = _Where()
    _add_text(where, filters, ["title"], ["username"] if viewer.is_admin else [])
    _dates(where, "at", filters)
    return sql, {"user": user}, where


def _bind(where: _Where, params: dict[str, object]) -> tuple[str, dict[str, object]]:
    """*where* with named parameters: one statement cannot mix both kinds."""
    sql = str(where)
    for i, value in enumerate(where.params):
        sql = sql.replace("?", f":v{i}", 1)
        params[f"v{i}"] = value
    return sql, params


def _read_plays(
    conn: sqlite3.Connection,
    viewer: Viewer,
    filters: Filters,
    cursor: _Cursor | None,
    limit: int,
) -> list[Play]:
    query = _plays_query(viewer, filters)
    if query is None:
        return []
    sql, params, where = query
    if cursor is not None and cursor.rank == _EVENT:
        where.add("at <= ?", cursor.at)
    elif cursor is not None:
        where.add("(at, ref) < (?, ?)", cursor.at, cursor.ref)
    condition, params = _bind(where, params)
    # rationale: *condition* is made of fixed fragments; every value is bound
    rows = conn.execute(  # nosec B608
        f"{sql} SELECT * FROM sittings WHERE {condition}"
        " ORDER BY at DESC, ref DESC LIMIT :row_limit",
        {**params, "row_limit": limit},
    ).fetchall()
    parts = [sorted(json.loads(r["parts"]), key=lambda p: (p[0], p[1])) for r in rows]
    numbers = _episode_numbers(conn, {p[4] for ps in parts for p in ps if p[4]})
    return [_play(r, ps, numbers) for r, ps in zip(rows, parts, strict=True)]


def _episode_numbers(
    conn: sqlite3.Connection, season_ids: set[str]
) -> dict[str, tuple[int | None, int | None]]:
    """The season and episode number of each episode of these seasons, by its key."""
    found: dict[str, tuple[int | None, int | None]] = {}
    rows = conn.execute(
        "SELECT season_number, episode_keys FROM media_items"
        " WHERE id IN (SELECT value FROM json_each(?)) AND episode_keys IS NOT NULL",
        (json.dumps(sorted(season_ids)),),
    )
    for row in rows:
        try:
            for episode in json.loads(row["episode_keys"]):
                found[str(episode["key"])] = (row["season_number"], episode.get("number"))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return found


def _play(
    row: sqlite3.Row,
    stored: list[list[object]],
    numbers: dict[str, tuple[int | None, int | None]],
) -> Play:
    parts: dict[str, PlayPart] = {}
    title_key: str | None = None
    for _at, ref, key, rating_key, _season_id, season, episode, name, finished in stored:
        title_key = str(key) if key else title_key
        if str(ref).startswith("t:"):
            continue
        part_key = str(rating_key or ref)
        # The last play of an episode says where it stands in the order, not what it lost.
        held = parts.pop(part_key, None)
        known = numbers.get(part_key, (None, None))
        is_tv = row["media_type"] == "tv"
        parts[part_key] = PlayPart(
            rating_key=str(rating_key) if rating_key else None,
            season_number=_first(season, known[0], held and held.season_number) if is_tv else None,
            episode_number=_first(episode, known[1], held and held.episode_number)
            if is_tv
            else None,
            title=str(name) if name else (held.title if held else None),
            finished=bool(finished) or (held is not None and held.finished),
        )
    last = list(parts.values())
    return Play(
        ref=row["ref"],
        username=row["username"],
        media_type=row["media_type"],
        title=row["title"].strip(),
        title_key=title_key,
        started_at=str(stored[0][0]),
        created_at=row["at"],
        finished=bool(last) and last[-1].finished,
        parts=tuple(last),
    )


def _first(*values: object) -> int | None:
    return next((v for v in values if isinstance(v, int)), None)


# --- the list ---


def _count(conn: sqlite3.Connection, viewer: Viewer, filters: Filters) -> int:
    total = 0
    joins = _JOINS if filters.q or filters.title else ""
    for where in _event_sources(viewer, filters):
        # rationale: *where* is made of fixed fragments; every value is bound
        total += conn.execute(  # nosec B608
            f"SELECT COUNT(*) FROM audit_log al {joins} WHERE {where}", where.params
        ).fetchone()[0]
    if query := _plays_query(viewer, filters):
        condition, params = _bind(query[2], query[1])
        # rationale: *condition* is made of fixed fragments; every value is bound
        total += conn.execute(  # nosec B608
            f"{query[0]} SELECT COUNT(*) FROM sittings WHERE {condition}", params
        ).fetchone()[0]
    return total


def history_page(
    conn: sqlite3.Connection,
    viewer: Viewer,
    filters: Filters,
    *,
    cursor: str | None = None,
    per_page: int = 25,
) -> Page:
    """A page of what *viewer* may see, the newest first.

    Raises BadCursor. New rows sort before every cursor, so they move no row between pages.
    """
    after = _decode(cursor) if cursor else None
    per_page = max(1, min(per_page, PER_PAGE_MAX))
    sources: list[list[Entry]] = [
        list(_read_events(conn, where, after, per_page + 1))
        for where in _event_sources(viewer, filters)
    ]
    sources.append(list(_read_plays(conn, viewer, filters, after, per_page + 1)))
    merged = list(heapq.merge(*sources, key=_order, reverse=True))
    entries = merged[:per_page]
    return Page(
        entries=entries,
        next_cursor=_encode(*_order(entries[-1])) if len(merged) > per_page else None,
        total=_count(conn, viewer, filters) if after is None else None,
    )
