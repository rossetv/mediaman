"""Reads and writes of ``plex_links`` and ``plex_plays``."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from mediaman.core.audit import security_event_or_raise
from mediaman.core.time import now_iso
from mediaman.services.infra import get_string_setting
from mediaman.services.plex_link.plex_tv import PlexIdentity

_CLIENT_ID_KEY = "plex_client_id"


class AlreadyLinked(Exception):
    """This person already has a linked Plex account."""


class AccountClaimed(Exception):
    """The Plex account is linked to someone else."""


@dataclass(frozen=True, slots=True)
class PlexLink:
    username: str
    server_account_id: int
    plex_name: str
    server_id: str
    linked_at: str
    last_read_at: str | None


@dataclass(frozen=True, slots=True)
class PlexPlay:
    """One play in a Plex app. Times are UTC ISO-8601 with an offset, whole seconds."""

    history_key: str
    media_type: str
    title: str
    viewed_at: str
    rating_key: str | None = None
    show_title: str | None = None
    show_rating_key: str | None = None
    season_number: int | None = None
    episode_number: int | None = None


def _link(row: sqlite3.Row) -> PlexLink:
    return PlexLink(
        username=row["username"],
        server_account_id=row["server_account_id"],
        plex_name=row["plex_name"],
        server_id=row["server_id"],
        linked_at=row["linked_at"],
        last_read_at=row["last_read_at"],
    )


def client_id(conn: sqlite3.Connection) -> str:
    """This install's identifier at plex.tv, made on first use."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            (_CLIENT_ID_KEY, str(uuid.uuid4()), now_iso()),
        )
    return get_string_setting(conn, _CLIENT_ID_KEY)


def get_link(conn: sqlite3.Connection, username: str) -> PlexLink | None:
    row = conn.execute("SELECT * FROM plex_links WHERE username = ?", (username,)).fetchone()
    return None if row is None else _link(row)


def list_links(conn: sqlite3.Connection) -> list[PlexLink]:
    rows = conn.execute("SELECT * FROM plex_links ORDER BY username")
    return [_link(r) for r in rows]


def linked_users(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Who has a link and since when: what an admin may see of other people's links."""
    rows = conn.execute(
        "SELECT u.id, u.username, l.linked_at FROM plex_links l"
        " JOIN admin_users u ON u.username = l.username ORDER BY u.username"
    )
    return [{"id": r["id"], "username": r["username"], "linked_at": r["linked_at"]} for r in rows]


def is_claimed_by_other(conn: sqlite3.Connection, identity: PlexIdentity, username: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM plex_links WHERE username != ?"
        " AND (plex_account_id = ? OR server_account_id = ?)",
        (username, identity.plex_account_id, identity.server_account_id),
    ).fetchone()
    return row is not None


def save_link(
    conn: sqlite3.Connection,
    username: str,
    identity: PlexIdentity,
    server_id: str,
    *,
    ip: str,
) -> None:
    """Link *identity* to *username* with its audit row, or raise and change nothing."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO plex_links (username, plex_account_id, server_account_id, plex_name,"
                " server_id, linked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    username,
                    identity.plex_account_id,
                    identity.server_account_id,
                    identity.name,
                    server_id,
                    now_iso(),
                ),
            )
            security_event_or_raise(conn, event="plex.linked", actor=username, ip=ip)
    except sqlite3.IntegrityError:
        if get_link(conn, username) is not None:
            raise AlreadyLinked from None
        if is_claimed_by_other(conn, identity, username):
            raise AccountClaimed from None
        raise


def unlink(
    conn: sqlite3.Connection,
    username: str,
    *,
    actor: str,
    ip: str = "",
    reason: str,
    server_id: str | None = None,
) -> bool:
    """Forget *username*'s link, the plays copied for it and the picks made from them.

    With *server_id*, only a link made on that server goes.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        link = get_link(conn, username)
        if link is None or (server_id is not None and link.server_id != server_id):
            return False
        conn.execute("DELETE FROM user_picks WHERE username = ? AND uses_plex = 1", (username,))
        conn.execute("DELETE FROM plex_plays WHERE username = ?", (username,))
        conn.execute("DELETE FROM plex_links WHERE username = ?", (username,))
        security_event_or_raise(
            conn,
            event="plex.unlinked",
            actor=actor,
            ip=ip,
            detail={"user": username, "reason": reason},
        )
    return True


def save_plays(
    conn: sqlite3.Connection,
    link: PlexLink,
    plays: Sequence[PlexPlay],
    *,
    read_at: datetime,
    oldest: datetime,
    keep: int,
) -> bool:
    """Store *plays* if *link* is still the person's link; False when it is not.

    Plays from before *oldest*, and all but the newest *keep*, are dropped.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get_link(conn, link.username)
        if current is None or (current.server_account_id, current.linked_at) != (
            link.server_account_id,
            link.linked_at,
        ):
            return False
        conn.executemany(
            "INSERT OR IGNORE INTO plex_plays (username, history_key, media_type, title,"
            " show_title, show_rating_key, season_number, episode_number, rating_key, viewed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    link.username,
                    p.history_key,
                    p.media_type,
                    p.title,
                    p.show_title,
                    p.show_rating_key,
                    p.season_number,
                    p.episode_number,
                    p.rating_key,
                    p.viewed_at,
                )
                for p in plays
            ],
        )
        conn.execute(
            "DELETE FROM plex_plays WHERE username = ? AND (viewed_at < ? OR history_key NOT IN"
            " (SELECT history_key FROM plex_plays WHERE username = ?"
            " ORDER BY viewed_at DESC, history_key DESC LIMIT ?))",
            (link.username, oldest.isoformat(), link.username, keep),
        )
        conn.execute(
            "UPDATE plex_links SET last_read_at = ? WHERE username = ?",
            (read_at.isoformat(), link.username),
        )
    return True
