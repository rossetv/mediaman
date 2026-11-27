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
    """Whether the token is that of a TV paired with *username*, among others or alone."""
    tv = find_by_token(conn, token_hash)
    return tv is not None and is_member(conn, tv.id, username)


def is_member(conn: sqlite3.Connection, tv_id: int, username: str) -> bool:
    """Whether *username* is one of the people the TV is paired with."""
    row = conn.execute(
        "SELECT 1 FROM tv_people p JOIN admin_users u ON u.id = p.user_id"
        " WHERE p.tv_id = ? AND u.username = ?",
        (tv_id, username),
    ).fetchone()
    return row is not None


def people(conn: sqlite3.Connection, tv_id: int) -> list[str]:
    """Everyone the TV is paired with, the first to pair it first."""
    rows = conn.execute(
        "SELECT u.username FROM tv_people p JOIN admin_users u ON u.id = p.user_id"
        " WHERE p.tv_id = ? ORDER BY p.paired_at, p.user_id",
        (tv_id,),
    )
    return [row[0] for row in rows]


def owned(conn: sqlite3.Connection, username: str) -> list[Tv]:
    """The TVs paired with *username*, each as theirs."""
    rows = conn.execute(
        f"{_SELECT} JOIN tv_people p ON p.tv_id = tvs.id"
        " JOIN admin_users me ON me.id = p.user_id WHERE me.username = ? ORDER BY tvs.id",
        (username,),
    )
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
    """Pair the TV with *username*: a new one under the token, or *again*, the TV's row where it was
    paired before.

    A TV paired with this person alone is paired anew under the token, so that nothing begun before
    reaches it. One that others are paired with keeps its token and what plays on it, and the person
    joins them. None is returned, and nothing changed, where *again* is no longer the TV's row as it
    stands. Raises ``SessionEnded`` unless the session is still the person's when it is stored.
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
        before = people(conn, again.id) if again is not None else []
        joined = bool(before) and username not in before
        if username not in before:
            (held,) = conn.execute(
                "SELECT COUNT(*) FROM tv_people WHERE user_id = ?", (user["id"],)
            ).fetchone()
            if held >= MAX_TVS_PER_PERSON:
                raise TooManyTvs("the person has too many TVs")
        if again is None:
            called = name or _DEFAULT_NAME
            tv_id = conn.execute(
                "INSERT INTO tvs (owner_id, kind, name, token_hash, created_at, last_seen_at)"
                " VALUES (?, 'page', ?, ?, ?, ?)",
                (user["id"], called, token_hash, at, at),
            ).lastrowid
        else:
            # The others' name for it stays.
            called, tv_id = again.name if joined else name or again.name, again.id
            kept = again.token_hash if before != [username] else token_hash
            changed = conn.execute(
                "UPDATE tvs SET name = ?, token_hash = ?, last_seen_at = ?"
                " WHERE id = ? AND token_hash = ?",
                (called, kept, at, tv_id, again.token_hash),
            )
            if changed.rowcount == 0:
                return None
        conn.execute(
            "INSERT OR IGNORE INTO tv_people (tv_id, user_id, paired_at) VALUES (?, ?, ?)",
            (tv_id, user["id"], at),
        )
        security_event_or_raise(
            conn,
            event="tv.joined" if joined else "tv.paired",
            actor=username,
            ip=ip,
            detail=_which(called, username),
        )
        # Where this fails, nothing is stored.
        return find(conn, tv_id or 0)


def rename(conn: sqlite3.Connection, tv: Tv, name: str, *, username: str) -> bool:
    """Name *tv*; False where it is another person's by now, and *username* no admin or its owner."""
    with conn:
        named = conn.execute(
            "UPDATE tvs SET name = ?1 WHERE id = ?2 AND owner_id IS ?3 AND EXISTS ("
            "SELECT 1 FROM admin_users u WHERE u.username = ?4"
            " AND (u.role = 'admin' OR EXISTS (SELECT 1 FROM tv_people p"
            " WHERE p.tv_id = tvs.id AND p.user_id = u.id)))",
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


def leave(conn: sqlite3.Connection, tv: Tv, username: str, *, actor: str, ip: str) -> bool:
    """Unpair *tv* from *username* alone, where others are paired with it too; False where they are
    not, or it is no longer the TV it was."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT p.user_id FROM tv_people p JOIN admin_users u ON u.id = p.user_id"
            " JOIN tvs ON tvs.id = p.tv_id WHERE p.tv_id = ? AND u.username = ?"
            " AND tvs.token_hash IS ?",
            (tv.id, username, tv.token_hash),
        ).fetchone()
        if user is None or len(people(conn, tv.id)) < 2:
            return False
        conn.execute(
            "DELETE FROM tv_people WHERE tv_id = ? AND user_id = ?", (tv.id, user["user_id"])
        )
        conn.execute(
            "UPDATE tvs SET owner_id = (SELECT p.user_id FROM tv_people p WHERE p.tv_id = tvs.id"
            " ORDER BY p.paired_at, p.user_id LIMIT 1) WHERE id = ? AND owner_id = ?",
            (tv.id, user["user_id"]),
        )
        security_event_or_raise(
            conn, event="tv.left", actor=actor, ip=ip, detail=_which(tv.name, username)
        )
    return True


def mark_seen(conn: sqlite3.Connection, tv_id: int, now: datetime) -> None:
    with conn:
        conn.execute("UPDATE tvs SET last_seen_at = ? WHERE id = ?", (now.isoformat(), tv_id))
