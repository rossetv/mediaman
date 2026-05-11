"""Admin-user CRUD that does not touch the bcrypt machinery.

Owns the "list users, delete users, and flip the
``must_change_password`` flag" concern.  Split from
:mod:`mediaman.web.auth.password_hash` so that file can focus on the
bcrypt-bound operations (create, authenticate, change_password) and
stay under the file-size ceiling.

Tests that monkey-patch ``mediaman.web.auth.password_hash.bcrypt`` are
unaffected: every helper in this module re-uses the standard library
for its plumbing only, and ``password_hash`` re-exports the public
names below for backwards-compatible imports.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TypedDict

logger = logging.getLogger(__name__)


class UserRecord(TypedDict):
    """A single admin user row returned by :func:`list_users`."""

    id: int
    username: str
    created_at: str


def user_must_change_password(conn: sqlite3.Connection, username: str) -> bool:
    """Return True when *username*'s account is flagged to force a rotation."""
    row = conn.execute(
        "SELECT must_change_password FROM admin_users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return False
    return bool(row["must_change_password"])


def set_must_change_password(conn: sqlite3.Connection, username: str, flag: bool) -> None:
    """Set / clear the force-rotation flag for *username*."""
    conn.execute(
        "UPDATE admin_users SET must_change_password = ? WHERE username = ?",
        (1 if flag else 0, username),
    )
    conn.commit()


def list_users(conn: sqlite3.Connection) -> list[UserRecord]:
    """Return all admin users (without password hashes)."""
    rows = conn.execute("SELECT id, username, created_at FROM admin_users ORDER BY id").fetchall()
    return [
        {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}
        for row in rows
    ]


def find_username_by_user_id(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Return the username for *user_id*, or None if no such user exists.

    The single sanctioned reader of ``admin_users.username`` outside the
    bcrypt-bound helpers in ``password_hash`` (§2.7.4: auth owns the users
    table; route handlers go through this module).
    """
    row = conn.execute(
        "SELECT username FROM admin_users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return row["username"] if row else None


def _delete_user_atomically(
    conn: sqlite3.Connection,
    user_id: int,
    target_username: str,
    *,
    audit_actor: str | None,
    audit_ip: str,
) -> bool:
    """Drop the user row + sessions + audit row in a single transaction.

    Returns ``True`` on success, ``False`` when the "last remaining
    admin" guard fires (the DELETE matched zero rows because the user
    would have been the last one).
    """
    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE preserves write-lock semantics. The _last_user
    # flag lets us return False AFTER the with-block has rolled back via
    # the sentinel raise.
    _last_user = False
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM admin_sessions WHERE username=?", (target_username,))
            cursor = conn.execute(
                "DELETE FROM admin_users WHERE id = ? AND (SELECT COUNT(*) FROM admin_users) > 1",
                (user_id,),
            )
            if cursor.rowcount == 0:
                _last_user = True
                raise RuntimeError("last_user")  # triggers with-block rollback
            if audit_actor is not None:
                from mediaman.core.audit import security_event_or_raise

                security_event_or_raise(
                    conn,
                    event="user.deleted",
                    actor=audit_actor,
                    ip=audit_ip,
                    detail={"target_id": user_id, "target_username": target_username},
                )
    except RuntimeError:
        if _last_user:
            return False
        raise
    return True


def _cleanup_reauth_tickets(conn: sqlite3.Connection, target_username: str) -> None:
    """Best-effort cleanup of reauth tickets held by a now-deleted user.

    Done outside the delete transaction so a tickets-table hiccup never
    blocks a successful user delete.
    """
    try:
        from mediaman.web.auth.reauth import revoke_all_reauth_for

        revoke_all_reauth_for(conn, target_username)
    except Exception:  # pragma: no cover — never break flow on cleanup failure
        logger.exception("delete_user reauth cleanup failed user=%s", target_username)


def delete_user(
    conn: sqlite3.Connection,
    user_id: int,
    current_username: str,
    *,
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> bool:
    """Delete an admin user by ID.

    Refuses to delete the current user or the last remaining admin.
    The session DELETE + user DELETE + audit row run inside a single
    ``BEGIN IMMEDIATE`` so we never have a "user vanished but no audit
    trail" outcome — see :func:`_delete_user_atomically`.
    """
    row = conn.execute("SELECT username FROM admin_users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return False
    if row["username"] == current_username:
        return False
    target_username = row["username"]

    if not _delete_user_atomically(
        conn,
        user_id,
        target_username,
        audit_actor=audit_actor,
        audit_ip=audit_ip,
    ):
        return False

    _cleanup_reauth_tickets(conn, target_username)
    return True
