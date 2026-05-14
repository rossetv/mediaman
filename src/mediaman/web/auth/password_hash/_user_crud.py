"""Admin-user CRUD on top of the bcrypt helpers.

Owns the create / list / delete operations and the per-user
notification-email and force-rotation flag mutations. The
authentication path lives in :mod:`._authenticate` and password
rotation in :mod:`._change_password`; this module is the "rows in
``admin_users``" concern.

Every state-changing function here follows the audit-in-transaction
pattern: when ``audit_actor`` is supplied, the ``sec:*`` row is written
inside the same ``BEGIN IMMEDIATE`` as the data change, so a failed
audit insert rolls the whole operation back — fail-closed.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TypedDict

import bcrypt

from mediaman.core.time import now_iso
from mediaman.web.auth._password_hash_helpers import (
    BCRYPT_ROUNDS,
    _LastUser,
    _prepare_bcrypt_input,
)

logger = logging.getLogger(__name__)


class UserExistsError(Exception):
    """Raised by :func:`create_user` when *username* is already taken.

    Callers can catch this specifically instead of a generic ``ValueError``
    so the HTTP layer can map it to a 409 without accidentally swallowing
    unrelated ``ValueError`` exceptions from deeper in the stack.
    """


class UserRecord(TypedDict):
    """A single admin user row returned by :func:`list_users`."""

    id: int
    username: str
    created_at: str
    email: str | None


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
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> None:
    """Insert an admin user with a bcrypt-hashed password.

    The bcrypt cost is :data:`BCRYPT_ROUNDS`. Passwords are routed
    through ``_prepare_bcrypt_input`` first so inputs over 72 bytes
    preserve full entropy (see package docstring).

    Audit-in-transaction: when *audit_actor* is supplied, a
    ``sec:user.created`` row is written inside the same
    ``BEGIN IMMEDIATE`` that inserts the user. If the audit insert
    blows up, the user-creation rolls back — we never have a "user
    minted but no audit trail exists" outcome.

    Raises:
        UserExistsError: *username* is already taken (mapped from the
            ``UNIQUE`` constraint on ``admin_users.username``).
        ValueError: *password* fails the strength policy — only when
            *enforce_policy* is true.
        sqlite3.IntegrityError: any other integrity-constraint failure
            that is not the username-uniqueness collision; it is
            re-raised unchanged after being logged so the caller sees
            the original error rather than a swallowed one.
    """
    if enforce_policy:
        from mediaman.web.auth.password_policy import password_issues

        issues = password_issues(password, username=username)
        if issues:
            raise ValueError("Password does not meet strength policy: " + "; ".join(issues))

    bcrypt_input = _prepare_bcrypt_input(password)
    password_hash = bcrypt.hashpw(bcrypt_input, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()
    now = now_iso()
    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics so the unique
    # username check and the INSERT are serialised.
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, created_at, must_change_password) "
                "VALUES (?, ?, ?, 0)",
                (username, password_hash, now),
            )
            if audit_actor is not None:
                from mediaman.core.audit import security_event_or_raise

                security_event_or_raise(
                    conn,
                    event="user.created",
                    actor=audit_actor,
                    ip=audit_ip,
                    detail={"new_username": username},
                )
    except sqlite3.IntegrityError as exc:
        message = (exc.args[0] if exc.args else "").lower()
        if "unique" in message and "admin_users.username" in message:
            raise UserExistsError(f"User '{username}' already exists") from exc
        logger.error("create_user integrity_error user=%s detail=%s", username, exc)
        raise


def list_users(conn: sqlite3.Connection) -> list[UserRecord]:
    """Return all admin users (without password hashes)."""
    rows = conn.execute(
        "SELECT id, username, created_at, email FROM admin_users ORDER BY id"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "username": row["username"],
            "created_at": row["created_at"],
            "email": row["email"],
        }
        for row in rows
    ]


def get_user_email(conn: sqlite3.Connection, username: str) -> str | None:
    """Return the notification email for *username*, or ``None`` if unset.

    Returns ``None`` for unknown usernames as well — callers cannot
    distinguish "no email set" from "user does not exist", which is
    intentional: every caller treats both cases identically (no email
    delivery).
    """
    row = conn.execute(
        "SELECT email FROM admin_users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return None
    # ``sqlite3.Row`` indexing is typed ``Any``; the column is declared
    # ``TEXT`` (nullable) so the narrowing is sound.
    email: str | None = row["email"]
    return email


def set_user_email(
    conn: sqlite3.Connection,
    username: str,
    email: str | None,
    *,
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> None:
    """Set or clear the notification email for *username*.

    Empty / whitespace-only strings collapse to ``NULL`` so the column
    always holds either ``NULL`` or a validated address. Validation is
    delegated to :func:`mediaman.core.email_validation.validate_email_address`,
    which raises ``ValueError`` on a malformed input.

    Unknown usernames silently no-op — callers are expected to gate on
    "current authenticated admin" before calling, so a missing row would
    mean the session points at a deleted user, which is handled upstream.
    This function does not enforce that invariant itself.

    Audit-in-transaction: when *audit_actor* is supplied, a
    ``sec:user.email_updated`` row is written inside the same
    ``BEGIN IMMEDIATE`` that updates the email column. If the audit
    insert fails, the entire UPDATE rolls back — we never have a "the
    email changed but no audit trail exists" outcome.
    """
    from mediaman.core.email_validation import validate_email_address

    normalised: str | None
    if email is None or not email.strip():
        normalised = None
    else:
        normalised = email.strip()
        validate_email_address(normalised)

    cleared = normalised is None

    if audit_actor is not None:
        from mediaman.core.audit import security_event_or_raise

        # ``with conn:`` commits on normal exit and rolls back on exception;
        # BEGIN IMMEDIATE here serialises the UPDATE and the audit INSERT so
        # an audit failure rolls back the email change — fail-closed.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE admin_users SET email = ? WHERE username = ?",
                (normalised, username),
            )
            security_event_or_raise(
                conn,
                event="user.email_updated",
                actor=audit_actor,
                ip=audit_ip,
                detail={"cleared": cleared},
            )
    else:
        conn.execute(
            "UPDATE admin_users SET email = ? WHERE username = ?",
            (normalised, username),
        )
        conn.commit()


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

    Audit-in-transaction: when *audit_actor* is supplied, a
    ``sec:user.deleted`` row is written inside the same
    ``BEGIN IMMEDIATE`` that drops the session and user rows. If the
    audit insert blows up, the entire delete rolls back — we never
    have a "user vanished but no audit trail" outcome.
    """
    row = conn.execute("SELECT username FROM admin_users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return False
    if row["username"] == current_username:
        return False
    target_username = row["username"]

    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics. Raising the
    # private ``_LastUser`` sentinel from inside the block triggers the
    # rollback and is caught immediately below — keeping the rollback
    # and the False-return in the same code path.
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM admin_sessions WHERE username=?", (target_username,))
            cursor = conn.execute(
                "DELETE FROM admin_users WHERE id = ? AND (SELECT COUNT(*) FROM admin_users) > 1",
                (user_id,),
            )
            if cursor.rowcount == 0:
                raise _LastUser(target_username)  # triggers with-block rollback
            if audit_actor is not None:
                from mediaman.core.audit import security_event_or_raise

                security_event_or_raise(
                    conn,
                    event="user.deleted",
                    actor=audit_actor,
                    ip=audit_ip,
                    detail={"target_id": user_id, "target_username": target_username},
                )
    except _LastUser:
        return False
    # Best-effort cleanup of any reauth tickets the deleted user held —
    # done outside the transaction so a tickets-table hiccup never
    # blocks a successful delete.
    # rationale: best-effort reauth revocation — the user row is already deleted;
    # a leftover ticket is a minor hygiene gap, not a security hole, and must
    # not roll back or block the successful delete response.
    # Narrowed to ``sqlite3.Error``: ``revoke_all_reauth_for`` is _ensure_table +
    # DELETE + commit, so a DB error is the only failure worth swallowing here. A
    # non-DB exception (a bad import, a ``TypeError``) means a bug and must surface.
    try:
        from mediaman.web.auth.reauth import revoke_all_reauth_for

        revoke_all_reauth_for(conn, target_username)
    except sqlite3.Error:  # pragma: no cover — never break flow on cleanup failure
        logger.exception("delete_user reauth cleanup failed user=%s", target_username)
    return True
