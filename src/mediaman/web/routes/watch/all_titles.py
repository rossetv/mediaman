"""All titles: the whole library as one grid, with its filters and orders.

A viewer's is what Watch holds. An admin's is what the scans know, as one title each, so a title
Watch cannot open is in it too; its cards carry what Keep and Delete need, and the filters Kept and
Stale and the orders by size, last watched and deletion are an admin's alone.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal, cast, get_args

from mediaman.core.format import format_bytes
from mediaman.core.time import now_iso, now_utc, parse_iso_strict_utc
from mediaman.scanner.runner import get_day_setting
from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.kept import fetch_keeps
from mediaman.web.repository.library_query import (
    DUE_SOON_DAYS,
    TitleSearch,
    days_ago,
    display_key,
    fetch_display_rows,
    row_items,
    shape_display_rows,
    title_ref,
)
from mediaman.web.routes.watch.library_order import library_page

# (value, label, admins only)
TYPES = (
    ("", "All", False),
    ("movies", "Movies", False),
    ("tv", "TV", False),
    ("anime", "Anime", False),
    ("unseen", "Not watched", False),
    ("kept", "Kept", True),
    ("stale", "Stale", True),
)
ADMIN_TYPES = frozenset(value for value, _, admin in TYPES if admin)
ADMIN_SORTS = frozenset(get_args(lib.AdminOrder))
DEFAULT_SORT: lib.TitleOrder = "added"
_NONE = {
    "movies": "No movies",
    "tv": "No TV shows",
    "anime": "No anime",
    "unseen": "You have watched everything",
    "kept": "Nothing is kept",
    "stale": "Nothing is stale",
}
_KINDS = {"movie": "Movie", "tv": "TV", "anime": "Anime"}

# The cards of titles Watch holds, by their refs.
type CardsOf = Callable[[list[str]], dict[str, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Library:
    """What All titles holds for one person; *rows* are an admin's, by key."""

    titles: list[lib.LibraryTitle]
    seen: set[str]
    rows: dict[str, sqlite3.Row] | None

    @property
    def admin(self) -> bool:
        return self.rows is not None


def in_one_read[T](conn: sqlite3.Connection, read: Callable[[], T]) -> T:
    """What *read* answers, of one moment: a keep made between two reads must not pass for the one
    a card shows."""
    if conn.in_transaction:
        return read()
    with conn:
        conn.execute("BEGIN")
        return read()


def known_type(value: str | None, admin: bool) -> str:
    """*value* when this person has that filter; else All."""
    known = {v for v, _, only in TYPES if admin or not only}
    return value if value in known else ""


def known_sort(value: str | None, admin: bool) -> lib.TitleOrder:
    """*value* when this person has that order; else Recently added."""
    orders = {*get_args(lib.RowOrder), *(ADMIN_SORTS if admin else ())}
    return cast(lib.TitleOrder, value) if value in orders else DEFAULT_SORT


def _held(
    movies: Sequence[lib.LibraryMovie], shows: Sequence[lib.LibraryShow]
) -> dict[str, lib.LibraryTitle]:
    titles = [
        lib.LibraryTitle(f"id:{m.key}", m.title, "movies", m.added_at, m.year, f"movie-{m.key}")
        for m in movies
    ] + [
        lib.LibraryTitle(
            f"show:{s.key}",
            s.title,
            "anime" if s.anime else "tv",
            s.added_at,
            s.year,
            f"show-{s.key}",
        )
        for s in shows
    ]
    return {t.key: t for t in titles}


def _opens(title: lib.LibraryTitle) -> bool:
    kind: Literal["movie", "show"] = "movie" if title.kind == "movies" else "show"
    return title_ref(kind, (title.ref or "").partition("-")[2]) is not None


def load(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    movies: Sequence[lib.LibraryMovie],
    shows: Sequence[lib.LibraryShow],
    *,
    admin: bool,
) -> Library:
    seen = lib.seen_refs(conn, username, libraries)
    held = _held(movies, shows)
    if not admin:
        return Library(list(held.values()), seen, None)
    rows = {display_key(r): r for r in fetch_display_rows(conn)}
    # By its items: the lowest of a row's ids need not be among the stale ones.
    key_of = {item: key for key, row in rows.items() for item in row_items(row)}
    stale = {key_of[r["id"]]: r["file_size_bytes"] or 0 for r in fetch_display_rows(conn, "stale")}
    titles = []
    for key, row in rows.items():
        kind = cast(Any, "movies" if row["display_type"] == "movie" else row["display_type"])
        own = held.get(key)
        # Nor can one be opened whose key Plex does not give.
        own = (
            own
            if own and _opens(own)
            else lib.LibraryTitle(key, row["title"], kind, row["added_at"] or "")
        )
        titles.append(
            replace(
                own,
                size_bytes=row["file_size_bytes"] or 0,
                watched_at=row["last_watched_at"],
                leaving_at=row["deletion_at"],
                kept=bool(row["is_kept"]),
                stale_bytes=stale.get(key),
            )
        )
    return Library(titles, seen, rows)


def _is_of(title: lib.LibraryTitle, type_: str, seen: set[str]) -> bool:
    if type_ == "unseen":
        return title.ref not in seen
    if type_ in ADMIN_TYPES:
        return title.kept if type_ == "kept" else title.stale_bytes is not None
    return not type_ or title.kind == type_


def _as_shown(library: Library, type_: str) -> list[lib.LibraryTitle]:
    """Every title as the filter *type_* shows it: Stale, at the size of what of it is stale."""
    if type_ != "stale":
        return library.titles
    return [
        t if t.stale_bytes is None else replace(t, size_bytes=t.stale_bytes) for t in library.titles
    ]


def _filtered(library: Library, type_: str) -> list[lib.LibraryTitle]:
    return [t for t in _as_shown(library, type_) if _is_of(t, type_, library.seen)]


def _fact(
    conn: sqlite3.Connection, titles: Sequence[lib.LibraryTitle], type_: str, now: datetime
) -> str:
    until = now + timedelta(days=DUE_SOON_DAYS)
    due = sum(1 for t in titles if (at := parse_iso_strict_utc(t.leaving_at)) and at <= until)
    said = [
        f"{format_bytes(sum(t.size_bytes for t in titles))} on disk",
        f"{due} leaving in the next {DUE_SOON_DAYS} days",
    ]
    if type_ == "stale":
        said.append(f"{get_day_setting(conn, 'min_age_days', default=30)}+ days, unwatched")
    return " · ".join(said)


def bar(conn: sqlite3.Connection, library: Library, type_: str, now: datetime) -> dict[str, Any]:
    """What stands over the grid: how many titles each filter has and, for an admin, the disk."""
    shown = _filtered(library, type_)
    return {
        "type": type_,
        "counts": {
            value: sum(1 for t in library.titles if _is_of(t, value, library.seen))
            for value, _, only in TYPES
            if library.admin or not only
        },
        "fact": _fact(conn, shown, type_, now) if library.admin else None,
        "total": len(shown),
        "empty": _empty(conn, type_, library.admin),
    }


def _empty(conn: sqlite3.Connection, type_: str, admin: bool) -> dict[str, str]:
    """What a grid of *type_* with nothing in it says."""
    if type_:
        title = _NONE[type_]
        return {
            "title": title,
            "link": "Show all titles",
            "href": "/watch?all=library",
            "filter": "",
        }
    if not admin:
        return {
            "title": "The library is empty for now",
            "link": "Back to all rows",
            "href": "/watch",
        }
    if not plex_is_configured(conn):
        return {
            "title": "Connect Plex to get started",
            "text": "Add your Plex URL and token in Settings.",
            "link": "Open Settings",
            "href": "/settings",
        }
    return {
        "title": "Your library is empty",
        "text": "Run a scan from Storage to pull in media from Plex.",
        "link": "Open Storage",
        "href": "/watch#storage",
    }


def _watched(title: lib.LibraryTitle) -> str:
    ago = days_ago(title.watched_at)
    return f"Watched {ago}" if ago else "Never watched"


def _leaves(title: lib.LibraryTitle, kept: bool, now: datetime) -> str:
    at = parse_iso_strict_utc(title.leaving_at)
    if kept:
        return "Kept"
    if at is None:
        return "Not scheduled"
    return "Leaving any day" if at < now else f"Leaves {at.day} {at:%b}"


def _caption(
    title: lib.LibraryTitle, item: dict[str, Any], sort: lib.TitleOrder, type_: str, now: datetime
) -> tuple[str | None, str | None]:
    """The line under the poster where it says what the grid goes by, and how it stands out."""
    kept = bool(item["protected"])
    if sort in ("size", "small"):
        return format_bytes(title.size_bytes) + (" · Kept" if kept else ""), None
    if sort in ("seen", "stalest"):
        said = _watched(title)
        return said, "bad" if said == "Never watched" else None
    if sort == "leaving":
        said = _leaves(title, kept, now)
        return said, "soon" if said.startswith("Lea") else None
    if type_ == "kept" and item["protection_label"]:
        return str(item["protection_label"]), None
    return None, None


def _admin_fields(
    title: lib.LibraryTitle, item: dict[str, Any], keep: object, now: datetime
) -> dict[str, Any]:
    """What an admin's Keep and Delete of the title need, as watch/admin.js takes them."""
    tv = bool(item["is_tv"])
    several = tv and item["season_count"] != 1
    delete: dict[str, Any] | None = None
    # A show of several seasons carries no item to delete, only the show whose seasons are listed.
    if not item["protected"]:
        delete = {"show_rk": item["show_rating_key"]}
        if not several:
            delete |= {
                "id": item["id"],
                "kind": "season" if tv else "movie",
                "size": item["file_size"],
            }
    unkeep = None
    if item["protected"]:
        # A show's keeps can sit on several seasons and on the show itself.
        unkeep = "dialog" if several or item["show_kept"] else "item"
    facts: list[str] = [format_bytes(title.size_bytes), f"{_watched(title)} by anyone"]
    if item["protection_label"]:
        facts.append(item["protection_label"])
    if title.leaving_at and not item["protected"]:
        facts.append(_leaves(title, False, now))
    return {
        "id": item["id"],
        "is_tv": tv,
        "show_rk": item["show_rating_key"],
        "has_rk": item["has_rating_key"],
        "show_title": item["show_title_raw"],
        "title": item["title"],
        "kept": item["protected"],
        "deletion": item["deletion_at"] is not None,
        "keep": keep,
        "seasons": item["season_count"],
        "has_keeps": bool(item["protection_label"]),
        "delete": delete,
        "unkeep": unkeep,
        "facts": " · ".join(facts),
    }


def _unheld_card(item: dict[str, Any]) -> dict[str, Any]:
    """A title Watch cannot open: a card that leads nowhere."""
    subtitle = _KINDS[item["media_type"]]
    key = item["plex_rating_key"]
    return {
        "kind": item["media_type"],
        "title": item["title"],
        "subtitle": subtitle,
        "poster_url": f"/api/poster/{key}" if key else None,
        "href": None,
        "watched": False,
        "mark_kind": None,
        "mark_key": None,
        "leaving_days": None,
        "keep_id": None,
    }


def _cards(
    conn: sqlite3.Connection,
    library: Library,
    page: list[lib.LibraryTitle],
    cards_of: CardsOf,
    sort: lib.TitleOrder,
    type_: str,
    now: datetime,
) -> list[dict[str, Any]]:
    held = cards_of([t.ref for t in page if t.ref])
    if library.rows is None:
        return [{**held[t.ref], "key": t.key} for t in page if t.ref in held]
    items = shape_display_rows(conn, [library.rows[t.key] for t in page])
    keeps = fetch_keeps(conn, [cast(str, i["id"]) for i in items], now_iso())
    cards = []
    for title, item in zip(page, items, strict=True):
        card = {**(held.get(title.ref or "") or _unheld_card(item)), "key": title.key}
        said, tone = _caption(title, item, sort, type_, now)
        if said:
            card["subtitle"] = said
        keep = keeps.get(cast(str, item["id"]))
        card["tone"] = tone
        card["admin"] = _admin_fields(title, item, asdict(keep) if keep else None, now)
        cards.append(card)
    return cards


def page_of(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    library: Library,
    cards_of: CardsOf,
    *,
    type_: str,
    sort: lib.TitleOrder,
    limit: int,
    now: datetime,
    after: str = "",
) -> dict[str, Any]:
    """A page of the titles of *type_* in the order *sort*, past *after*, and the bar over them."""
    titles = _filtered(library, type_)
    page, end = library_page(conn, username, libraries, titles, sort, limit, after)
    items = _cards(conn, library, page, cards_of, sort, type_, now)
    return {"items": items, "next": end, **bar(conn, library, type_, now)}


def one_of(
    conn: sqlite3.Connection,
    library: Library,
    cards_of: CardsOf,
    key: str,
    *,
    type_: str,
    sort: lib.TitleOrder,
    now: datetime,
) -> dict[str, Any]:
    """The card of the title *key* as it is now, none when it is gone, and the bar."""
    titles = [t for t in _as_shown(library, type_) if t.key == key]
    items = _cards(conn, library, titles, cards_of, sort, type_, now)
    return {"items": items, "next": None, **bar(conn, library, type_, now)}


def unheld(
    conn: sqlite3.Connection, username: str, libraries: list[str], search: TitleSearch
) -> list[dict[str, Any]]:
    """An admin's cards of the titles *search* finds that Watch cannot open."""
    # The whole library is read for them, which a search is too often for with none there.
    if not conn.execute(
        "SELECT 1 FROM media_items WHERE CAST(plex_library_id AS TEXT) NOT IN"
        " (SELECT CAST(value AS TEXT) FROM json_each(?))"
        " OR (media_type != 'movie' AND show_rating_key IS NULL) LIMIT 1",
        (json.dumps(libraries),),
    ).fetchone():
        return []
    movies, shows = lib.library_movies(conn, libraries), lib.library_shows(conn, libraries)
    library = load(conn, username, libraries, movies, shows, admin=True)
    held = _held(movies, shows)
    found = [
        t for t in library.titles if t.key not in held and search.distance(t.title) is not None
    ]
    return _cards(conn, library, found, lambda _refs: {}, DEFAULT_SORT, "", now_utc())


__all__ = [
    "ADMIN_SORTS",
    "ADMIN_TYPES",
    "TYPES",
    "Library",
    "bar",
    "in_one_read",
    "known_sort",
    "known_type",
    "load",
    "one_of",
    "page_of",
    "unheld",
]
