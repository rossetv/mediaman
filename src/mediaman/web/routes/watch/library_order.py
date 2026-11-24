"""A library row's pages in any of its orders.

Not watched first goes by the person's own marks, which change while they page. Each first page
holds what they had seen, and the place it ends at names it, so a mark made meanwhile neither
repeats a title nor skips one. A place whose marks are no longer held (a restart, or too many
since) goes on by the marks of now.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Sequence

from mediaman.web.repository import watch_library as lib

HELD_MAX = 64

_lock = threading.Lock()
# (username, the refs they had seen) by the name their places give it, oldest used first.
_held: OrderedDict[str, tuple[str, frozenset[str]]] = OrderedDict()


def _seen_at(
    conn: sqlite3.Connection, username: str, libraries: list[str], name: str
) -> tuple[str, frozenset[str]]:
    """What *username* had seen when the pages *name* stands for began, and its name."""
    with _lock:
        held = _held.get(name)
        if held is not None and held[0] == username:
            _held.move_to_end(name)
            return name, held[1]
    return secrets.token_urlsafe(12), frozenset(lib.seen_refs(conn, username, libraries))


def _hold(name: str, username: str, seen: frozenset[str]) -> None:
    with _lock:
        _held[name] = (username, seen)
        _held.move_to_end(name)
        while len(_held) > HELD_MAX:
            _held.popitem(last=False)


def is_place(text: str) -> bool:
    return lib.parse_cursor(text.partition(".")[0]) is not None


def library_page[T: lib.LibraryMovie | lib.LibraryShow](
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    entries: Sequence[T],
    order: lib.RowOrder,
    limit: int,
    after: str = "",
) -> tuple[list[T], str | None]:
    """The *limit* of *entries* past *after* (a place this gave, in the same *order*), and the
    place they end at; None at the end."""
    place, _, name = after.partition(".")
    cursor = lib.parse_cursor(place) if place else None
    seen: frozenset[str] = frozenset()
    if order == "unwatched":
        name, seen = _seen_at(conn, username, libraries, name)
    page, end = lib.row_page(lib.in_order(entries, order, seen), cursor, limit, order, seen)
    if end is None:
        return page, None
    if order != "unwatched":
        return page, lib.cursor_text(end)
    _hold(name, username, seen)
    return page, f"{lib.cursor_text(end)}.{name}"


__all__ = ["HELD_MAX", "is_place", "library_page"]
