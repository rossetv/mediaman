"""Each person's avatar: the row behind it, and the URL a screen shows it at.

A change answers with the file it left unused, which the caller removes once this is committed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from mediaman.core.time import now_iso
from mediaman.services.avatars import drawn


@dataclass(frozen=True)
class Avatar:
    user_id: int
    choice: int | None = None
    """The drawing the person chose."""
    file_name: str | None = None
    """Their photo; with neither, they have their default."""

    @property
    def url(self) -> str:
        if self.file_name is not None:
            return f"/api/avatars/{self.user_id}?v={self.version}"
        return drawn.url(self.choice or drawn.default_choice(self.user_id))

    @property
    def version(self) -> str:
        """Names this photo in its URL without telling where it is kept."""
        return (self.file_name or "")[:12]


def get_avatar(conn: sqlite3.Connection, user_id: int) -> Avatar:
    """The avatar of *user_id*, a default one if they set none or no longer exist."""
    row = conn.execute(
        "SELECT choice, file_name FROM user_avatars WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return Avatar(user_id)
    return _avatar(user_id, row)


def _avatar(user_id: int, row: sqlite3.Row) -> Avatar:
    choice: int | None = row["choice"]
    if choice is not None and choice > drawn.COUNT:
        choice = None
    return Avatar(user_id, choice, row["file_name"])


def avatar_urls(conn: sqlite3.Connection, user_ids: Iterable[int]) -> dict[int, str]:
    """The avatar URL of each of *user_ids*, for a list of people."""
    wanted = set(user_ids)
    rows = {
        row["user_id"]: row
        for row in conn.execute("SELECT user_id, choice, file_name FROM user_avatars")
    }
    return {
        user_id: (_avatar(user_id, rows[user_id]) if user_id in rows else Avatar(user_id)).url
        for user_id in wanted
    }


def avatar_url(conn: sqlite3.Connection, username: str) -> str | None:
    """The avatar URL of *username*, or ``None`` when there is no such person."""
    user_id = find_user_id(conn, username)
    return None if user_id is None else get_avatar(conn, user_id).url


def find_user_id(conn: sqlite3.Connection, username: str) -> int | None:
    row = conn.execute("SELECT id FROM admin_users WHERE username = ?", (username,)).fetchone()
    return None if row is None else row["id"]


def set_drawn(conn: sqlite3.Connection, user_id: int, choice: int) -> str | None:
    return _set(conn, user_id, "drawn", choice, None)


def set_upload(conn: sqlite3.Connection, user_id: int, file_name: str) -> str | None:
    return _set(conn, user_id, "upload", None, file_name)


def _set(
    conn: sqlite3.Connection, user_id: int, kind: str, choice: int | None, file_name: str | None
) -> str | None:
    """Raises ``sqlite3.IntegrityError`` when the person was deleted in the meantime."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        unused = get_avatar(conn, user_id).file_name
        conn.execute(
            "INSERT INTO user_avatars (user_id, kind, choice, file_name, updated_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET kind = excluded.kind,"
            " choice = excluded.choice, file_name = excluded.file_name,"
            " updated_at = excluded.updated_at",
            (user_id, kind, choice, file_name, now_iso()),
        )
    return unused


def clear(conn: sqlite3.Connection, user_id: int) -> str | None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        unused = get_avatar(conn, user_id).file_name
        conn.execute("DELETE FROM user_avatars WHERE user_id = ?", (user_id,))
    return unused


def file_names(conn: sqlite3.Connection) -> set[str]:
    """Every photo in use."""
    return {
        row["file_name"]
        for row in conn.execute("SELECT file_name FROM user_avatars WHERE file_name IS NOT NULL")
    }
