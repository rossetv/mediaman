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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast

from mediaman.core.audit import CLI_UNLOCK, UNSIGNED_ACTIONS
from mediaman.core.time import parse_iso_strict_utc
from mediaman.scanner.repository.audit import _FILTER_MAP, SHOW_ACTIONS, AuditRow
from mediaman.web.history_text import HIDDEN, event_title

PLAYS = "plays"
#: What each filter group holds beside plays, which are no events: actions, and the beginnings
#: of actions, which end in a separator.
GROUPS: dict[str, tuple[str, ...]] = {
    PLAYS: (),
    "downloads": ("downloaded", "re_downloaded", "sec:auto_abandon."),
    "keeps": (*_FILTER_MAP["kept"], *_FILTER_MAP["unkept"]),
    "deletions": (
        "scheduled",
        *_FILTER_MAP["scheduled"],
        "deleted",
        "delete_",
        "dry_run_skip",
        "media_missing",
        "unmonitor_skipped",
    ),
    "sign_ins": (
        "sec:login.",
        "sec:logout",
        "sec:session.",
        "sec:password.",
        "sec:reauth.",
        "sec:plex.",
    ),
    "people": ("sec:user.",),
    "settings": ("sec:settings.", "sec:aes."),
    "scans": ("sec:scan.", "sec:library.", "library_left_out", "scanned"),
    "newsletter": ("sec:newsletter.", "sec:subscriber."),
}
#: The groups that can hold what a person who is no admin did.
OWN_GROUPS = (PLAYS, "downloads", "keeps", "sign_ins")
PER_PAGE_MAX = 100
#: What a filter or a search is cut to.
TEXT_MAX = 200
_TITLE_MAX = 1000
_NAME_MAX = 300
#: The most titles a menu lists.
TITLES_MAX = 100
_CURSOR_MAX = 300
_ID_MAX = 2**63 - 1
#: Plays of one title further apart than this are two sittings.
_SITTING_DAYS = 4 / 24
_WINDOW_MARGIN = timedelta(hours=5)
_WINDOW_DAYS = (30, 180)

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
    """*since* is inclusive and *until* exclusive; *action* is a group or a filter's name.

    *user* is a Who's value, *title* a whole title.
    """

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
    #: Where the last play in mediaman stopped, and how long the film or episode is.
    position_s: float | None = None
    duration_s: float | None = None


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
    #: Of a film the library still holds.
    year: int | None = None
    runtime_s: float | None = None


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


# In an address a name stands for itself, but one that starts with the mark gets another before
# it: what the mark starts alone is never an account's name, now or later.
_MARK = "~"
_SYSTEM_VALUE = _MARK + "mediaman"
_FORMER_PREFIX = _MARK + "former:"
Kind = Literal["account", "former", "system"]


@dataclass(frozen=True, slots=True)
class Who:
    """Whose an event is: an account's, a removed account's, or mediaman's own.

    A name that was an account's, was removed and is now another's is two Whos.
    """

    kind: Kind
    name: str

    @property
    def value(self) -> str:
        """What stands for this Who in an address."""
        if self.kind == "system":
            return _SYSTEM_VALUE
        if self.kind == "former":
            return _FORMER_PREFIX + self.name
        return self.name if not self.name.startswith(_MARK) else _MARK + self.name


SYSTEM = Who("system", "mediaman")


def who_of(conn: sqlite3.Connection, value: str) -> Who | None:
    """The Who of an address's *value*; None when it is none. A name with no account is a
    former user's."""
    if value == _SYSTEM_VALUE:
        return SYSTEM
    if value.startswith(_FORMER_PREFIX):
        return Who("former", value.removeprefix(_FORMER_PREFIX))
    name = value.removeprefix(_MARK) if value.startswith(_MARK * 2) else value
    if name.startswith(_MARK) and name == value:
        return None
    return Who("account" if viewer_for(conn, name) else "former", name)


def account_names(conn: sqlite3.Connection, viewer: Viewer) -> list[str]:
    """The accounts *viewer* may see: all for an admin, else only their own."""
    if not viewer.is_admin:
        return [viewer.username]
    rows = conn.execute("SELECT username FROM admin_users ORDER BY username COLLATE NOCASE")
    return [r["username"] for r in rows]


def people(conn: sqlite3.Connection, viewer: Viewer) -> list[Who]:
    """Whom *viewer* may filter by: for an admin every account, then the former users who left
    events, then mediaman."""
    found = [Who("account", name) for name in account_names(conn, viewer)]
    if not viewer.is_admin:
        return found
    rows = conn.execute(
        f"SELECT DISTINCT al.actor FROM audit_log al {_WHO_JOIN}"
        f" WHERE al.actor IS NOT NULL AND {_WHO_KIND} = 'former' ORDER BY al.actor COLLATE NOCASE"
    )
    return [*found, *(Who("former", r["actor"]) for r in rows), SYSTEM]


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
        user=_text(user, _NAME_MAX),
        action=_text(action),
        # Only spaces, which are what SQLite's TRIM takes off the stored titles.
        title=_text(title, _TITLE_MAX, " "),
        since=_moment(since),
        until=_moment(until, end=True),
    )


def _text(value: str, limit: int = TEXT_MAX, outer: str | None = None) -> str:
    return value.replace("\0", "").encode(errors="ignore").decode().strip(outer)[:limit]


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


def _add_search(
    where: _Where, text: str, titles: Sequence[str], names: Sequence[str], only: str = ""
) -> None:
    """*names* are what the search reads beside the titles; *only* goes with a title match."""
    if not text:
        return
    sql = only + _any_like(titles)
    if names:
        sql = f"(({sql}) OR {_any_like(names)})"
    where.add(sql, *[_like(text)] * (len(titles) + len(names)))


_SHOW_ACTIONS_SQL = ",".join(f"'{a}'" for a in SHOW_ACTIONS)

_JOINS = f"""
LEFT JOIN media_items mi
  ON mi.id = al.media_item_id AND al.action NOT IN ({_SHOW_ACTIONS_SQL})
    AND al.media_item_id != '_security'
LEFT JOIN kept_shows ks
  ON ks.show_rating_key = al.media_item_id AND al.action IN ({_SHOW_ACTIONS_SQL})
"""

# The title an event is listed under; empty when it has none.
_TITLE = (
    "COALESCE(NULLIF(TRIM(mi.title), ''), NULLIF(TRIM(ks.show_title), ''),"
    " history_title(al.action, al.media_item_id, al.detail))"
)


def _add_functions(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("SELECT history_title('', '', NULL)").close()
    except sqlite3.OperationalError:
        conn.create_function("history_title", 3, event_title, deterministic=True)


_EVENT_COLUMNS = """
al.id, al.media_item_id, al.action, al.detail, al.space_reclaimed_bytes, al.created_at, al.actor,
mi.title AS mi_title, mi.plex_rating_key, ks.show_title AS ks_title
"""


# Whose an event is. A row without an account's id is that name's account's only from when the
# account was made.
_UNSIGNED_SQL = ",".join(f"'{a}'" for a in UNSIGNED_ACTIONS)
_IS_SYSTEM = (
    f"(COALESCE(al.actor, '') = '' OR al.action IN ({_UNSIGNED_SQL})"
    f" OR (al.action = '{CLI_UNLOCK[0]}' AND al.actor = '{CLI_UNLOCK[1]}'))"
)
_IS_ACCOUNTS = (
    "COALESCE(CASE WHEN al.actor_id IS NULL"
    " THEN julianday(al.created_at) >= julianday(wu.created_at)"
    " ELSE al.actor_id = wu.id END, 0)"
)
_WHO_JOIN = "LEFT JOIN admin_users wu ON wu.username = al.actor"
_WHO_KIND = (
    f"CASE WHEN {_IS_SYSTEM} THEN 'system' WHEN {_IS_ACCOUNTS} THEN 'account' ELSE 'former' END"
)


def _scopes(viewer: Viewer) -> list[_Where]:
    """The events *viewer* may see, as conditions that never match the same row."""
    return [_Where()] if viewer.is_admin else _account_scopes(viewer)


def _account_scopes(account: Viewer) -> list[_Where]:
    """The events of one account, as conditions that never match the same row."""
    own = _Where()
    own.add("al.actor_id = ?", account.user_id)
    # Rows without an account's id: theirs by name, but not a person's before them of that name.
    # The plus keeps the read on the index of names, not on every row without an id.
    named = _Where()
    named.add("+al.actor_id IS NULL AND al.actor = ?", account.username)
    named.add("julianday(al.created_at) >= julianday(?)", account.created_at)
    named.add(f"NOT {_IS_SYSTEM}")
    return [own, named]


def _filtered_scopes(conn: sqlite3.Connection, viewer: Viewer, filters: Filters) -> list[_Where]:
    """The scopes of *viewer*, narrowed to the user filtered by."""
    if not filters.user:
        return _scopes(viewer)
    who = who_of(conn, filters.user)
    if who is None or (not viewer.is_admin and who != Who("account", viewer.username)):
        return []
    where = _Where()
    if who.kind == "system":
        where.add(_IS_SYSTEM)
    elif who.kind == "account" and (account := viewer_for(conn, who.name)):
        return _account_scopes(account)
    else:
        where.add("al.actor = ?", who.name)
        where.add(f"(SELECT {_WHO_KIND} FROM (SELECT 1) {_WHO_JOIN}) = 'former'")
    return [where]


def _event_filters(where: _Where, viewer: Viewer, filters: Filters) -> None:
    if filters.action == "security":
        where.add("al.action >= 'sec:' AND al.action < 'sec;'")
    elif filters.action in GROUPS:
        _in_group(where, GROUPS[filters.action])
    elif filters.action:
        actions = _FILTER_MAP.get(filters.action, (filters.action,))
        where.add(f"al.action IN ({','.join('?' * len(actions))})", *actions)
    titles = ["mi.title", "mi.show_title", "ks.show_title", "al.media_item_id"]
    if viewer.is_admin:
        # A removed title is named only in the text, which can also hold what is not a person's
        # own to find by.
        titles.append("al.detail")
    if filters.title:
        # A title the text names is in the text, unless part of it is hidden: most texts need no
        # closer look.
        within = "%" if HIDDEN in filters.title else _like(filters.title)
        where.add(
            "COALESCE(NULLIF(TRIM(mi.title), ''), NULLIF(TRIM(ks.show_title), ''),"
            " CASE WHEN al.detail LIKE ? ESCAPE '\\'"
            " THEN history_title(al.action, al.media_item_id, al.detail) END) = ? COLLATE NOCASE",
            within,
            filters.title,
        )
    _add_search(
        where,
        filters.q,
        titles,
        ["al.actor"] if viewer.is_admin else [],
        only="al.media_item_id != '_security' AND ",
    )
    _dates(where, "al.created_at", filters)


def _in_group(where: _Where, actions: Sequence[str]) -> None:
    starts = [a for a in actions if a[-1] in "._"]
    whole = [a for a in actions if a not in starts]
    sql = [
        f"al.action IN ({','.join('?' * len(whole))})",
        *["(al.action >= ? AND al.action < ?)"] * len(starts),
    ]
    bounds = [b for s in starts for b in (s, s[:-1] + chr(ord(s[-1]) + 1))]
    where.add(f"({' OR '.join(sql)})", *whole, *bounds)


def _event_sources(conn: sqlite3.Connection, viewer: Viewer, filters: Filters) -> list[_Where]:
    if filters.action == PLAYS:
        return []
    if filters.title:
        _add_functions(conn)
    scopes = _filtered_scopes(conn, viewer, filters)
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
         wp.watched = 1 AND (wp.seen_at IS NULL OR wp.seen_at <= wp.updated_at) AS finished,
         NULLIF(wp.position_s, 0) AS position_s, NULLIF(wp.duration_s, 0) AS duration_s
  FROM watch_progress wp
  JOIN admin_users u ON u.username = wp.username
  LEFT JOIN media_items mi ON mi.id = wp.media_item_id
  WHERE wp.updated_at > '1971' AND {wp_user} AND {wp_window}
    AND (wp.position_s > 0 OR wp.watched = 1 OR wp.credited_at IS NOT NULL)
  UNION ALL
  SELECT p.username, CASE p.media_type WHEN 'movie' THEN 'movie' ELSE 'tv' END,
         CASE p.media_type WHEN 'movie' THEN p.title ELSE p.show_title END,
         CASE p.media_type WHEN 'movie' THEN p.rating_key ELSE p.show_rating_key END,
         p.viewed_at, 'x:' || p.rowid, p.rating_key, NULL, p.season_number, p.episode_number,
         CASE p.media_type WHEN 'movie' THEN NULL ELSE p.title END, 1,
         NULL, NULL
  FROM plex_plays p
  JOIN admin_users u ON u.username = p.username
  JOIN plex_links l ON l.username = p.username
  WHERE p.media_type IN ('movie', 'episode') AND {p_user} AND {p_window}
  UNION ALL
  SELECT t.username, t.media_type, t.title, NULL, t.played_at, 't:' || t.rowid, NULL, NULL, NULL,
         NULL, NULL, 0, NULL, NULL
  FROM played_titles t
  JOIN admin_users u ON u.username = t.username
  WHERE {t_user} AND {t_window}
),
marked AS (
  SELECT *, julianday(at) - julianday(LAG(at) OVER one_title) > {gap} AS starts,
         LEAD(at) OVER one_title IS NULL
           OR COALESCE(julianday(LEAD(at) OVER one_title) - julianday(at), 0) > {gap} AS ends
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
                                     episode_number, episode_title, finished, position_s,
                                     duration_s)) AS parts
  FROM numbered GROUP BY username, media_type, LOWER(title), sitting
)
"""


def _plays_query(
    conn: sqlite3.Connection,
    viewer: Viewer,
    filters: Filters,
    window: tuple[str, str] | None = None,
) -> tuple[str, dict[str, object], _Where] | None:
    """The statement's start, what it binds and what its sittings must match.

    With *window*, only plays stored between its two moments are read.
    """
    if filters.action not in ("", PLAYS):
        return None
    user = None if viewer.is_admin else viewer.username
    if filters.user:
        who = who_of(conn, filters.user)
        if who is None or who.kind != "account" or user not in (None, who.name):
            return None
        user = who.name
    scoped = "{0}.username = :user" if user is not None else "1"
    windowed = "{0} >= :low AND {0} <= :high" if window else "1"
    sql = _PLAYS_SQL.format(
        wp_user=scoped.format("wp"),
        p_user=scoped.format("p"),
        t_user=scoped.format("t"),
        wp_window=windowed.format("wp.updated_at"),
        p_window=windowed.format("p.viewed_at"),
        t_window=windowed.format("t.played_at"),
        gap=_SITTING_DAYS,
    )
    params: dict[str, object] = {"user": user}
    if window:
        params |= {"low": window[0], "high": window[1]}
    where = _Where()
    if filters.title:
        where.add("TRIM(title) = ? COLLATE NOCASE", filters.title)
    _add_search(where, filters.q, ["title"], ["username"] if viewer.is_admin else [])
    _dates(where, "at", filters)
    return sql, params, where


def _bind(where: _Where, params: dict[str, object]) -> tuple[str, dict[str, object]]:
    """*where* with named parameters: one statement cannot mix both kinds."""
    sql = str(where)
    for i, value in enumerate(where.params):
        sql = sql.replace("?", f":v{i}", 1)
        params[f"v{i}"] = value
    return sql, params


def _windows(
    conn: sqlite3.Connection, filters: Filters, cursor: _Cursor | None
) -> list[tuple[str, str] | None]:
    """Ever longer spans to look for a page's sittings in, the last one all time.

    Each reaches past its newest sitting by more than a sitting's gap, so a play that a later
    one follows is never taken for the end of its sitting.
    """
    if filters.q or filters.title:
        return [None]
    newest = cursor.at if cursor else None
    if newest is None:
        newest = conn.execute(
            "SELECT MAX(at) FROM (SELECT MAX(updated_at) AS at FROM watch_progress"
            " UNION ALL SELECT MAX(viewed_at) FROM plex_plays"
            " UNION ALL SELECT MAX(played_at) FROM played_titles)"
        ).fetchone()[0]
    end = parse_iso_strict_utc(newest)
    if end and filters.until:
        end = min(end, filters.until)
    if end is None:
        return [None]
    try:
        high = (end + _WINDOW_MARGIN).isoformat()
        spans = [((end - timedelta(days=d)).isoformat(), high) for d in _WINDOW_DAYS]
    except OverflowError:
        return [None]
    return [*spans, None]


def _read_plays(
    conn: sqlite3.Connection,
    viewer: Viewer,
    filters: Filters,
    cursor: _Cursor | None,
    limit: int,
) -> list[Play]:
    plays: list[Play] = []
    for window in _windows(conn, filters, cursor):
        plays = _read_window(conn, viewer, filters, cursor, limit, window)
        if window is None or len(plays) < limit:
            continue
        # A sitting that reaches the start of the span may have begun before it.
        edge = (datetime.fromisoformat(window[0]) + _WINDOW_MARGIN).isoformat()
        if all(p.started_at > edge for p in plays):
            break
    return plays


def _read_window(
    conn: sqlite3.Connection,
    viewer: Viewer,
    filters: Filters,
    cursor: _Cursor | None,
    limit: int,
    window: tuple[str, str] | None,
) -> list[Play]:
    query = _plays_query(conn, viewer, filters, window)
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
    plays = [_play(r, ps, numbers) for r, ps in zip(rows, parts, strict=True)]
    films = _films(conn, {p.title_key for p in plays if p.media_type == "movie" and p.title_key})
    return [_with_film(p, films) for p in plays]


def _films(conn: sqlite3.Connection, keys: set[str]) -> dict[str, tuple[int | None, int | None]]:
    """The year and the minutes of each of these films the library holds."""
    rows = conn.execute(
        "SELECT mi.id, mi.year, (SELECT CASE WHEN json_valid(tm.details)"
        " THEN json_extract(tm.details, '$.runtime') END FROM title_meta tm"
        " WHERE tm.id_type = 'movie' AND tm.id = mi.tmdb_id) AS minutes"
        " FROM media_items mi WHERE mi.id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(keys)),),
    )
    return {r["id"]: (_first(r["year"]), _first(r["minutes"])) for r in rows}


def _with_film(play: Play, films: dict[str, tuple[int | None, int | None]]) -> Play:
    if play.media_type != "movie":
        return play
    year, minutes = films.get(play.title_key or "", (None, None))
    stored = next((p.duration_s for p in play.parts if p.duration_s), None)
    told = minutes * 60.0 if minutes and minutes > 0 else None
    return replace(play, year=year, runtime_s=stored or told)


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
    for _at, ref, key, rating_key, _season_id, season, episode, name, finished, pos, dur in stored:
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
            position_s=_seconds(pos) or (held.position_s if held else None),
            duration_s=_seconds(dur) or (held.duration_s if held else None),
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


def _seconds(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


# --- the list ---


def _joins(filters: Filters) -> str:
    """What counting needs beside the events: titles, only when they are looked for."""
    return _JOINS if filters.q or filters.title else ""


def _count(conn: sqlite3.Connection, viewer: Viewer, filters: Filters) -> int:
    total = 0
    joins = _joins(filters)
    for where in _event_sources(conn, viewer, filters):
        # rationale: *where* is made of fixed fragments; every value is bound
        total += conn.execute(  # nosec B608
            f"SELECT COUNT(*) FROM audit_log al {joins} WHERE {where}", where.params
        ).fetchone()[0]
    if query := _plays_query(conn, viewer, filters):
        condition, params = _bind(query[2], query[1])
        # rationale: *condition* is made of fixed fragments; every value is bound
        total += conn.execute(  # nosec B608
            f"{query[0]} SELECT COUNT(*) FROM marked WHERE ends AND {condition}", params
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

    Raises BadCursor. New rows sort before every cursor, so they move no row between pages. A
    title played again between two pages moves to its new time, before the cursor: if its page
    was still to come, it is not in this walk, only in the next.
    """
    after = _decode(cursor) if cursor else None
    per_page = max(1, min(per_page, PER_PAGE_MAX))
    sources: list[list[Entry]] = [
        list(_read_events(conn, where, after, per_page + 1))
        for where in _event_sources(conn, viewer, filters)
    ]
    sources.append(list(_read_plays(conn, viewer, filters, after, per_page + 1)))
    merged = list(heapq.merge(*sources, key=_order, reverse=True))
    entries = merged[:per_page]
    return Page(
        entries=entries,
        next_cursor=_encode(*_order(entries[-1])) if len(merged) > per_page else None,
        total=_count(conn, viewer, filters) if after is None else None,
    )


# --- the filters' menus ---


@dataclass(frozen=True, slots=True)
class TitleCount:
    title: str
    count: int


def _tally(
    conn: sqlite3.Connection, viewer: Viewer, filters: Filters, key: str, join: str = ""
) -> list[sqlite3.Row]:
    """The events *filters* match, counted by *key*: two columns named k1 and k2."""
    rows: list[sqlite3.Row] = []
    joins = _joins(filters)
    for where in _event_sources(conn, viewer, filters):
        # rationale: every fragment is fixed; every value is bound
        rows += conn.execute(  # nosec B608
            f"SELECT {key}, COUNT(*) AS n FROM audit_log al {joins} {join} WHERE {where}"
            " GROUP BY k1, k2",
            where.params,
        ).fetchall()
    return rows


def _tally_plays(
    conn: sqlite3.Connection, viewer: Viewer, filters: Filters, key: str
) -> list[sqlite3.Row]:
    query = _plays_query(conn, viewer, filters)
    if query is None:
        return []
    condition, params = _bind(query[2], query[1])
    # rationale: every fragment is fixed; every value is bound
    return conn.execute(  # nosec B608
        f"{query[0]} SELECT {key} AS k1, COUNT(*) AS n FROM marked WHERE ends AND {condition}"
        " GROUP BY k1",
        params,
    ).fetchall()


def user_counts(
    conn: sqlite3.Connection, viewer: Viewer, filters: Filters
) -> list[tuple[Who, int]]:
    """Everyone *viewer* may filter by, with how many rows are theirs."""
    filters = replace(filters, user="")
    counts: dict[Who, int] = {}
    rows = _tally(conn, viewer, filters, f"{_WHO_KIND} AS k1, al.actor AS k2", _WHO_JOIN)
    for row in rows:
        kind = cast(Kind, row["k1"])
        who = SYSTEM if kind == "system" else Who(kind, row["k2"])
        counts[who] = counts.get(who, 0) + row["n"]
    for row in _tally_plays(conn, viewer, filters, "username"):
        who = Who("account", row["k1"])
        counts[who] = counts.get(who, 0) + row["n"]
    return [(who, counts.get(who, 0)) for who in people(conn, viewer)]


def action_counts(conn: sqlite3.Connection, viewer: Viewer, filters: Filters) -> dict[str, int]:
    """Each group *viewer* may filter by, with how many rows it holds."""
    filters = replace(filters, action="")
    groups = GROUPS if viewer.is_admin else OWN_GROUPS
    counts = dict.fromkeys(groups, 0)
    for row in _tally(conn, viewer, filters, "al.action AS k1, NULL AS k2"):
        group = group_of(row["k1"])
        if group in counts:
            counts[group] += row["n"]
    counts[PLAYS] = sum(r["n"] for r in _tally_plays(conn, viewer, filters, "username"))
    return counts


def group_of(action: str) -> str | None:
    for group, actions in GROUPS.items():
        if any(action.startswith(a) if a[-1] in "._" else action == a for a in actions):
            return group
    return None


def title_counts(
    conn: sqlite3.Connection, viewer: Viewer, filters: Filters, find: str = ""
) -> list[TitleCount]:
    """The titles with the most rows, at most TITLES_MAX of them, that hold *find*."""
    filters = replace(filters, title="")
    _add_functions(conn)
    find = _text(find)
    rows: list[sqlite3.Row] = []
    for where in _event_sources(conn, viewer, filters):
        # rationale: every fragment is fixed; every value is bound
        rows += conn.execute(  # nosec B608
            "SELECT LOWER(name) AS k1, name, COUNT(*) AS n FROM ("
            f"SELECT {_TITLE} AS name FROM audit_log al {_JOINS} WHERE {where}"
            " AND al.media_item_id != '_security' AND al.action != 'library_left_out'"
            # What keeps the title from being worked out again for each use of it.
            " LIMIT -1) WHERE name != '' AND name LIKE ? ESCAPE '\\' GROUP BY k1",
            (*where.params, _like(find)),
        ).fetchall()
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for row in rows:
        counts[row["k1"]] = counts.get(row["k1"], 0) + row["n"]
        names.setdefault(row["k1"], row["name"])
    play_rows = _tally_plays(conn, viewer, filters, "TRIM(title)")
    for row in play_rows:
        key = _ascii_lower(row["k1"])
        if _ascii_lower(find) not in key:
            continue
        counts[key] = counts.get(key, 0) + row["n"]
        names.setdefault(key, row["k1"])
    best = sorted(counts, key=lambda k: (-counts[k], k))[:TITLES_MAX]
    return [TitleCount(names[k], counts[k]) for k in best]


def _ascii_lower(text: str) -> str:
    """As SQLite folds case, so that a count and the filter it stands for agree."""
    return "".join(c.lower() if c.isascii() else c for c in text)


def whose(conn: sqlite3.Connection, entries: Sequence[Entry]) -> dict[int | str, Who]:
    """Whose each of these entries is, by an event's id or a play's ref."""
    found: dict[int | str, Who] = {
        e.ref: Who("account", e.username) for e in entries if isinstance(e, Play)
    }
    ids = [e.id for e in entries if not isinstance(e, Play)]
    rows = conn.execute(
        f"SELECT al.id, al.actor, {_WHO_KIND} AS kind FROM audit_log al {_WHO_JOIN}"
        " WHERE al.id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    )
    for row in rows:
        kind = cast(Kind, row["kind"])
        found[row["id"]] = SYSTEM if kind == "system" else Who(kind, row["actor"])
    return found
