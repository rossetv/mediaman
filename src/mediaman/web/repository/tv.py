"""Repository functions for the TVs to play on, and who they belong to."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from mediaman.core.audit import security_event_or_raise

MAX_TVS_PER_PERSON = 20
_DEFAULT_NAME = "TV"


class SessionEnded(Exception):
    """The session that asks has ended."""


class TooManyTvs(Exception):
    """The person has as many TVs as a person may."""


@dataclass(frozen=True, slots=True)
class Tv:
    id: int
    kind: str
    name: str
    #: None for an older TV, which is everyone's.
    owner: str | None
    owner_id: int | None
    token_hash: str | None
    paired_at: str
    last_seen_at: str | None


_SELECT = (
    "SELECT tvs.id, tvs.kind, tvs.name, u.username AS owner, tvs.owner_id, tvs.token_hash,"
    " tvs.created_at AS paired_at, tvs.last_seen_at"
    " FROM tvs LEFT JOIN admin_users u ON u.id = tvs.owner_id"
)


def _tv(row: sqlite3.Row | None) -> Tv | None:
    return Tv(**dict(row)) if row is not None else None


def find(conn: sqlite3.Connection, tv_id: int) -> Tv | None:
    return _tv(conn.execute(f"{_SELECT} WHERE tvs.id = ?", (tv_id,)).fetchone())


def find_by_token(conn: sqlite3.Connection, token_hash: str) -> Tv | None:
    """The paired TV that holds the token."""
    sql = f"{_SELECT} WHERE tvs.token_hash = ?"
    return _tv(conn.execute(sql, (token_hash,)).fetchone())


def is_paired(conn: sqlite3.Connection, token_hash: str, username: str) -> bool:
    """Whether the token is that of a TV paired with *username*."""
    tv = find_by_token(conn, token_hash)
    return tv is not None and tv.owner == username


def owned(conn: sqlite3.Connection, username: str) -> list[Tv]:
    rows = conn.execute(f"{_SELECT} WHERE u.username = ? ORDER BY tvs.id", (username,))
    return [Tv(**dict(row)) for row in rows]


def every(conn: sqlite3.Connection) -> list[Tv]:
    return [Tv(**dict(row)) for row in conn.execute(f"{_SELECT} ORDER BY tvs.id")]


def has_session(conn: sqlite3.Connection, username: str, session_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM admin_sessions WHERE token_hash = ? AND username = ?",
        (session_hash, username),
    ).fetchone()
    return row is not None


def _which(name: str, owner: str | None) -> dict[str, object]:
    """A TV and whose it is, by the names the history shows as they stand."""
    return {"device": name, "user": owner} if owner is not None else {"device": name}


def pair(
    conn: sqlite3.Connection,
    *,
    username: str,
    session_hash: str,
    token_hash: str,
    name: str | None,
    again: Tv | None,
    ip: str,
    now: datetime,
) -> Tv | None:
    """Pair the TV that holds the token with *username*.

    *again* is the TV's row where it was paired before; None is returned, and nothing changed,
    where that is no longer whose it was. Raises ``SessionEnded`` unless the session is still the
    person's when it is stored.
    """
    at = now.isoformat()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not has_session(conn, username, session_hash):
            raise SessionEnded("the session has ended")
        user = conn.execute(
            "SELECT id FROM admin_users WHERE username = ? AND must_change_password = 0",
            (username,),
        ).fetchone()
        if user is None:
            return None
        if again is None or again.owner != username:
            (held,) = conn.execute(
                "SELECT COUNT(*) FROM tvs WHERE owner_id = ?", (user["id"],)
            ).fetchone()
            if held >= MAX_TVS_PER_PERSON:
                raise TooManyTvs("the person has too many TVs")
        # Someone else's name for it stays theirs.
        kept = again.name if again is not None and again.owner == username else _DEFAULT_NAME
        called = name or kept
        if again is None:
            tv_id = conn.execute(
                "INSERT INTO tvs (owner_id, kind, name, token_hash, created_at, last_seen_at)"
                " VALUES (?, 'page', ?, ?, ?, ?)",
                (user["id"], called, token_hash, at, at),
            ).lastrowid
        else:
            tv_id = again.id
            changed = conn.execute(
                "UPDATE tvs SET owner_id = ?, name = ?, token_hash = ?, created_at = ?,"
                " last_seen_at = ? WHERE id = ? AND token_hash = ?",
                (user["id"], called, token_hash, at, at, tv_id, again.token_hash),
            )
            if changed.rowcount == 0:
                return None
        taken = again is not None and again.owner != username
        was = (again.name, again.owner) if again is not None and taken else (called, username)
        security_event_or_raise(
            conn,
            event="tv.taken_over" if taken else "tv.paired",
            actor=username,
            ip=ip,
            detail=_which(*was),
        )
        # Where this fails, nothing is stored.
        return find(conn, tv_id or 0)


def rename(conn: sqlite3.Connection, tv: Tv, name: str, *, username: str) -> bool:
    """Name *tv*; False where it is another person's by now, and *username* no admin or its owner."""
    with conn:
        named = conn.execute(
            "UPDATE tvs SET name = ?1 WHERE id = ?2 AND owner_id IS ?3 AND EXISTS ("
            "SELECT 1 FROM admin_users u WHERE u.username = ?4"
            " AND (u.role = 'admin' OR u.id = tvs.owner_id))",
            (name, tv.id, tv.owner_id, username),
        )
    return named.rowcount > 0


def remove(conn: sqlite3.Connection, tv: Tv, *, actor: str, ip: str) -> bool:
    """Unpair *tv*; False where it was unpaired or paired anew meanwhile."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        gone = conn.execute(
            "DELETE FROM tvs WHERE id = ? AND token_hash IS ?", (tv.id, tv.token_hash)
        )
        if gone.rowcount:
            security_event_or_raise(
                conn, event="tv.unpaired", actor=actor, ip=ip, detail=_which(tv.name, tv.owner)
            )
    return gone.rowcount > 0


def mark_seen(conn: sqlite3.Connection, tv_id: int, now: datetime) -> None:
    with conn:
        conn.execute("UPDATE tvs SET last_seen_at = ? WHERE id = ?", (now.isoformat(), tv_id))
