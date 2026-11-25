"""Repository functions for the older TVs an admin added by address, which are everyone's."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from mediaman.core.audit import security_event_or_raise

MAX_OLDER_TVS = 20


class TooManyOlderTvs(Exception):
    """There are as many older TVs as there may be."""


class AlreadyAdded(Exception):
    """A TV with that address, or the same device, is there already."""


@dataclass(frozen=True, slots=True)
class OlderTv:
    id: int
    name: str
    address: str
    #: What the TV calls itself to other devices; empty where it said nothing.
    udn: str
    added_at: str
    last_seen_at: str | None


_SELECT = (
    "SELECT id, name, address, COALESCE(udn, '') AS udn, created_at AS added_at, last_seen_at"
    " FROM tvs WHERE kind = 'older'"
)


def every(conn: sqlite3.Connection) -> list[OlderTv]:
    return [OlderTv(**dict(row)) for row in conn.execute(f"{_SELECT} ORDER BY id")]


def find(conn: sqlite3.Connection, tv_id: int) -> OlderTv | None:
    row = conn.execute(f"{_SELECT} AND id = ?", (tv_id,)).fetchone()
    return OlderTv(**dict(row)) if row is not None else None


def same(conn: sqlite3.Connection, address: str, udn: str) -> OlderTv | None:
    """The TV that has *address*, or is the same device."""
    row = conn.execute(
        f"{_SELECT} AND (address = ? OR (udn = ? AND udn <> '')) ORDER BY id LIMIT 1",
        (address, udn),
    ).fetchone()
    return OlderTv(**dict(row)) if row is not None else None


def add(
    conn: sqlite3.Connection,
    *,
    name: str,
    address: str,
    udn: str,
    actor: str,
    ip: str,
    now: datetime,
) -> int:
    """Add the TV, which has just answered, and return its id."""
    at = now.isoformat()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if same(conn, address, udn) is not None:
            raise AlreadyAdded
        (held,) = conn.execute("SELECT COUNT(*) FROM tvs WHERE kind = 'older'").fetchone()
        if held >= MAX_OLDER_TVS:
            raise TooManyOlderTvs
        tv_id = conn.execute(
            "INSERT INTO tvs (kind, name, address, udn, created_at, last_seen_at)"
            " VALUES ('older', ?, ?, ?, ?, ?)",
            (name, address, udn, at, at),
        ).lastrowid
        security_event_or_raise(
            conn, event="tv.added", actor=actor, ip=ip, detail={"address": address}
        )
    return tv_id or 0


def rename(conn: sqlite3.Connection, tv_id: int, name: str) -> bool:
    with conn:
        changed = conn.execute(
            "UPDATE tvs SET name = ? WHERE id = ? AND kind = 'older'", (name, tv_id)
        )
    return changed.rowcount > 0


def move(conn: sqlite3.Connection, tv_id: int, address: str) -> bool:
    """Keep the TV's new address; False where another TV has it."""
    with conn:
        moved = conn.execute(
            "UPDATE tvs SET address = ? WHERE id = ? AND kind = 'older' AND NOT EXISTS"
            " (SELECT 1 FROM tvs o WHERE o.kind = 'older' AND o.address = ? AND o.id <> ?)",
            (address, tv_id, address, tv_id),
        )
    return moved.rowcount > 0


def mark_seen(conn: sqlite3.Connection, tv_id: int, now: datetime) -> None:
    with conn:
        conn.execute(
            "UPDATE tvs SET last_seen_at = ? WHERE id = ? AND kind = 'older'",
            (now.isoformat(), tv_id),
        )


def remove(conn: sqlite3.Connection, tv_id: int, *, actor: str, ip: str) -> bool:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        gone = conn.execute("DELETE FROM tvs WHERE id = ? AND kind = 'older'", (tv_id,))
        if gone.rowcount:
            security_event_or_raise(conn, event="tv.removed", actor=actor, ip=ip)
    return gone.rowcount > 0
