"""Bcrypt password hashing, verification, and rotation.

Split from ``auth/session.py`` (R2). Owns the "how are passwords hashed
and compared" concern; session persistence lives in
:mod:`mediaman.auth.session_store`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import TypedDict

import bcrypt

from mediaman.services.infra.time import now_iso

logger = logging.getLogger("mediaman")


class UserRecord(TypedDict):
    """A single admin user row returned by :func:`list_users`."""

    id: int
    username: str
    created_at: str


_DUMMY_HASH: bytes | None = None
_DUMMY_HASH_LOCK = threading.Lock()


def _get_dummy_hash() -> bytes:
    """Lazily compute the bcrypt dummy hash the first time it's needed."""
    global _DUMMY_HASH
    with _DUMMY_HASH_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12))
        return _DUMMY_HASH


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


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    enforce_policy: bool = True,
) -> None:
    """Insert an admin user with a bcrypt-hashed password (cost 12)."""
    if enforce_policy:
        from mediaman.auth.password_policy import password_issues

        issues = password_issues(password, username=username)
        if issues:
            raise ValueError("Password does not meet strength policy: " + "; ".join(issues))

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    now = now_iso()
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at, must_change_password) "
            "VALUES (?, ?, ?, 0)",
            (username, password_hash, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        message = (exc.args[0] if exc.args else "").lower()
        if "unique" in message and "admin_users.username" in message:
            raise ValueError(f"User '{username}' already exists") from exc
        logger.error("create_user integrity_error user=%s detail=%s", username, exc)
        raise


def authenticate(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    record_failures: bool = True,
) -> bool:
    """Verify username/password credentials.

    Always performs a bcrypt check — even for nonexistent users — to
    prevent timing-based username enumeration.
    """
    from mediaman.auth.login_lockout import (
        check_lockout,
        record_failure,
        record_success,
    )

    locked = bool(username) and check_lockout(conn, username)
    if locked:
        bcrypt.checkpw(password.encode(), _get_dummy_hash())
        if record_failures:
            record_failure(conn, username)
        logger.warning("auth.account_locked user=%s reason=lockout_active", username)
        return False

    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    ).fetchone()

    if row is None:
        bcrypt.checkpw(password.encode(), _get_dummy_hash())
        if username and record_failures:
            record_failure(conn, username)
        return False

    ok = bcrypt.checkpw(password.encode(), row["password_hash"].encode())
    if ok:
        record_success(conn, username)
    elif record_failures:
        record_failure(conn, username)
    return ok


def change_password(
    conn: sqlite3.Connection,
    username: str,
    old_password: str,
    new_password: str,
    *,
    enforce_policy: bool = True,
) -> bool:
    """Change a user's password. Returns True on success, False if old password is wrong."""
    if not authenticate(conn, username, old_password, record_failures=False):
        return False

    if enforce_policy:
        from mediaman.auth.password_policy import password_issues

        issues = password_issues(new_password, username=username)
        if issues:
            raise ValueError("Password does not meet strength policy: " + "; ".join(issues))

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    conn.execute(
        "UPDATE admin_users SET password_hash=?, must_change_password=0 WHERE username=?",
        (new_hash, username),
    )
    conn.execute("DELETE FROM admin_sessions WHERE username=?", (username,))
    conn.commit()
    logger.info("password.changed user=%s sessions_revoked=all", username)
    return True


def list_users(conn: sqlite3.Connection) -> list[UserRecord]:
    """Return all admin users (without password hashes)."""
    rows = conn.execute("SELECT id, username, created_at FROM admin_users ORDER BY id").fetchall()
    return [
        {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}
        for row in rows
    ]


def delete_user(conn: sqlite3.Connection, user_id: int, current_username: str) -> bool:
    """Delete an admin user by ID.

    Refuses to delete the current user or the last remaining admin.
    """
    row = conn.execute("SELECT username FROM admin_users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return False
    if row["username"] == current_username:
        return False

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM admin_sessions WHERE username=?", (row["username"],))
        cursor = conn.execute(
            "DELETE FROM admin_users WHERE id = ? AND (SELECT COUNT(*) FROM admin_users) > 1",
            (user_id,),
        )
        if cursor.rowcount == 0:
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True
