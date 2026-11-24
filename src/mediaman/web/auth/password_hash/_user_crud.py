"""Admin-user CRUD on top of the bcrypt helpers.

Owns the create / list / delete operations and the per-user
notification-email and force-rotation flag mutations. The
authentication path lives in `._authenticate` and password
rotation in `._change_password`; this module is the "rows in
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
from mediaman.scanner.repository.watch_credit import keep_credited_plays
from mediaman.web.auth._password_hash_helpers import (
    BCRYPT_ROUNDS,
    _LastUser,
    _prepare_bcrypt_input,
    _sanitise_log_field,
)
from mediaman.web.auth.login_lockout import admin_unlock

logger = logging.getLogger(__name__)


class UserExistsError(Exception):
    """Raised by `create_user` when *username* is already taken.

    Callers can catch this specifically instead of a generic ``ValueError``
    so the HTTP layer can map it to a 409 without accidentally swallowing
    unrelated ``ValueError`` exceptions from deeper in the stack.
    """


class LastAdminError(Exception):
    """Raised by `set_user_role` when it would leave the instance with no admin."""


class EmailTakenError(Exception):
    """Raised by `set_user_email` when the address is someone else's."""


ROLES = ("admin", "viewer")


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")


class UserRecord(TypedDict):
    id: int
    username: str
    created_at: str
    email: str | None
    role: str


def user_must_change_password(conn: sqlite3.Connection, username: str) -> bool:
    row = conn.execute(
        "SELECT must_change_password FROM admin_users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return False
    return bool(row["must_change_password"])


def set_must_change_password(conn: sqlite3.Connection, username: str, flag: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE admin_users SET must_change_password = ? WHERE username = ?",
            (1 if flag else 0, username),
        )


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    enforce_policy: bool = True,
    role: str = "admin",
    must_change_password: bool = False,
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> None:
    """Insert a user (an admin unless *role* says ``viewer``) with a bcrypt-hashed password.

    *must_change_password* flags the account so the first sign-in is sent to the
    password-change page; it is written in the same insert as the row.

    The bcrypt cost is `BCRYPT_ROUNDS`. Passwords are routed
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
            *enforce_policy* is true — or *role* is not ``admin`` / ``viewer``.
        sqlite3.IntegrityError: any other integrity-constraint failure
            that is not the username-uniqueness collision; it is
            re-raised unchanged after being logged so the caller sees
            the original error rather than a swallowed one.
    """
    _check_role(role)
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
                "INSERT INTO admin_users "
                "(username, password_hash, created_at, must_change_password, role) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, password_hash, now, 1 if must_change_password else 0, role),
            )
            if audit_actor is not None:
                from mediaman.core.audit import security_event_or_raise

                security_event_or_raise(
                    conn,
                    event="user.created",
                    actor=audit_actor,
                    ip=audit_ip,
                    detail={"new_username": username, "role": role},
                )
    except sqlite3.IntegrityError as exc:
        message = (exc.args[0] if exc.args else "").lower()
        if "unique" in message and "admin_users.username" in message:
            raise UserExistsError(f"User '{username}' already exists") from exc
        logger.error(
            "create_user integrity_error user=%s detail=%s", _sanitise_log_field(username), exc
        )
        raise


def list_users(conn: sqlite3.Connection) -> list[UserRecord]:
    rows = conn.execute(
        "SELECT id, username, created_at, email, role FROM admin_users ORDER BY id"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "username": row["username"],
            "created_at": row["created_at"],
            "email": row["email"],
            "role": row["role"],
        }
        for row in rows
    ]


def get_user_role(conn: sqlite3.Connection, username: str) -> str | None:
    """Return ``"admin"`` or ``"viewer"`` for *username*, or ``None`` for an unknown user."""
    row = conn.execute("SELECT role FROM admin_users WHERE username = ?", (username,)).fetchone()
    if row is None:
        return None
    role: str = row["role"]
    return role


def is_last_admin(conn: sqlite3.Connection, username: str) -> bool:
    """True when *username* is an admin and no other admin exists."""
    row = conn.execute(
        "SELECT role = 'admin' AND (SELECT COUNT(*) FROM admin_users WHERE role = 'admin') = 1 "
        "FROM admin_users WHERE username = ?",
        (username,),
    ).fetchone()
    return bool(row and row[0])


def set_user_role(
    conn: sqlite3.Connection,
    username: str,
    role: str,
    *,
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> bool:
    """Change *username*'s role; return ``False`` when there is no such user.

    Audit-in-transaction like the other mutations here: a failed ``sec:user.role_changed``
    insert rolls the change back.

    Raises:
        ValueError: *role* is not ``admin`` / ``viewer``.
        LastAdminError: the change would demote the only remaining admin.
    """
    _check_role(role)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        # The count and the UPDATE share one statement under the write lock, so two
        # admins demoting each other at once cannot both pass.
        cursor = conn.execute(
            "UPDATE admin_users SET role = ? WHERE username = ? AND (? = 'admin' OR role != 'admin' "
            "OR (SELECT COUNT(*) FROM admin_users WHERE role = 'admin') > 1)",
            (role, username, role),
        )
        if cursor.rowcount == 0:
            exists = conn.execute(
                "SELECT 1 FROM admin_users WHERE username = ?", (username,)
            ).fetchone()
            if exists is None:
                return False
            raise LastAdminError(username)  # rolls the with-block back
        if audit_actor is not None:
            from mediaman.core.audit import security_event_or_raise

            security_event_or_raise(
                conn,
                event="user.role_changed",
                actor=audit_actor,
                ip=audit_ip,
                detail={"target_username": username, "role": role},
            )
    return True


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
    allow_subscriber: bool = False,
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> None:
    """Set or clear the notification email for *username*.

    Empty / whitespace-only strings collapse to ``NULL`` so the column
    always holds either ``NULL`` or a validated address. Validation is
    delegated to `mediaman.core.email_validation.validate_email_address`,
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

    Raises:
        ValueError: the address is malformed.
        EmailTakenError: another user has the address, or *username* is not an
            admin and the newsletter goes to it, unless *allow_subscriber* says
            an admin vouches for it. An address kept as it is always passes.
    """
    from mediaman.core.email_validation import validate_email_address

    normalised: str | None
    if email is None or not email.strip():
        normalised = None
    else:
        normalised = email.strip()
        validate_email_address(normalised)

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = get_user_email(conn, username)
        if normalised is not None and _address_is_taken(
            conn, username, normalised, previous, allow_subscriber=allow_subscriber
        ):
            raise EmailTakenError
        conn.execute(
            "UPDATE admin_users SET email = ? WHERE username = ?",
            (normalised, username),
        )
        if audit_actor is not None:
            from mediaman.core.audit import security_event_or_raise

            detail: dict[str, object] = {
                "cleared": normalised is None,
                "email": normalised,
                "previous": previous,
            }
            if audit_actor != username:
                detail["target_username"] = username
            security_event_or_raise(
                conn, event="user.email_updated", actor=audit_actor, ip=audit_ip, detail=detail
            )


def _address_is_taken(
    conn: sqlite3.Connection,
    username: str,
    email: str,
    previous: str | None,
    *,
    allow_subscriber: bool,
) -> bool:
    # Nobody proves an address is theirs, and one person's picks and notices follow it.
    if previous is not None and previous.lower() == email.lower():
        return False
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM admin_users WHERE email = ? COLLATE NOCASE"
        " AND username != ?)"
        " OR (NOT ? AND EXISTS (SELECT 1 FROM subscribers WHERE email = ? COLLATE NOCASE"
        " AND active = 1)"
        " AND EXISTS (SELECT 1 FROM admin_users WHERE username = ? AND role != 'admin'))",
        (email, username, allow_subscriber, email, username),
    ).fetchone()
    return bool(row[0])


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
            keep_credited_plays(conn, target_username)
            # A user created later under this name must not inherit this
            # one's failure count, lock or requests.
            admin_unlock(conn, target_username)
            conn.execute(
                "UPDATE download_notifications SET username = NULL WHERE username = ?",
                (target_username,),
            )
            cursor = conn.execute(
                "DELETE FROM admin_users WHERE id = ? AND (role != 'admin' "
                "OR (SELECT COUNT(*) FROM admin_users WHERE role = 'admin') > 1)",
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
        logger.exception(
            "delete_user reauth cleanup failed user=%s", _sanitise_log_field(target_username)
        )
    return True
