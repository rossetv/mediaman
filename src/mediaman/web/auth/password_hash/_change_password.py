"""Password rotation — `change_password` and its guards.

Splits the rotation into three parts so the ``with conn:`` block in
`change_password` stands alone and its "all these writes share
one transaction" rationale is literally true of what remains:

* `_authorise_password_change` — the pre-transaction guard
  sequence (reauth-lockout check, old-password verification, new-
  password policy). Runs before anything is written.
* `change_password` — the transactional UPDATE + session purge +
  reauth-ticket purge + audit, plus TOCTOU handling.
* `_clear_reauth_failure_counter` — the best-effort post-
  transaction failure-counter cleanup.

Beside them, what the screens tell of it: `is_same_password`,
`change_lock` and `password_changed_at`.

User CRUD lives in `._user_crud`; the credential-verification
path in `._authenticate`.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime
from typing import NamedTuple

import bcrypt

from mediaman.core.time import now_utc, parse_iso_strict_utc, parse_iso_utc
from mediaman.web.auth._password_hash_helpers import (
    BCRYPT_ROUNDS,
    _get_dummy_hash,
    _prepare_bcrypt_input,
    _sanitise_log_field,
    _UserVanished,
)
from mediaman.web.auth.login_lockout import is_locked_out, one_attempt_at_a_time, reauth_key
from mediaman.web.auth.password_hash._authenticate import verify_password

logger = logging.getLogger(__name__)


def is_same_password(one: str, other: str) -> bool:
    """Whether a hash of *one* verifies *other*."""
    return _prepare_bcrypt_input(one) == _prepare_bcrypt_input(other)


class ChangeLock(NamedTuple):
    """Too many wrong passwords: no change is made, whatever the password."""

    #: None when the stored end cannot be read.
    seconds_left: int | None


def change_lock(conn: sqlite3.Connection, username: str) -> ChangeLock | None:
    """The lock `change_password` refuses *username* by; None when there is none.

    Only for the signed-in person themselves: it tells that the account exists.
    """
    key = reauth_key(username)
    if not is_locked_out(conn, key):
        return None
    row = conn.execute(
        "SELECT locked_until FROM login_failures WHERE username = ?", (key,)
    ).fetchone()
    until = parse_iso_strict_utc(row["locked_until"]) if row else None
    if until is None:
        return ChangeLock(None)
    return ChangeLock(max(1, math.ceil((until - now_utc()).total_seconds())))


def password_changed_at(conn: sqlite3.Connection, username: str) -> datetime | None:
    """When *username* last changed their password; None when the history has no change."""
    row = conn.execute(
        # A row without an account's id is theirs by name, but not from before the account was.
        "SELECT al.created_at FROM audit_log al JOIN admin_users u ON u.username = al.actor"
        " WHERE u.username = ?"
        " AND al.action IN ('sec:password.changed', 'sec:password.forced_changed')"
        " AND CASE WHEN al.actor_id IS NULL"
        " THEN julianday(al.created_at) >= julianday(u.created_at) ELSE al.actor_id = u.id END"
        " ORDER BY al.id DESC LIMIT 1",
        (username,),
    ).fetchone()
    return parse_iso_utc(row["created_at"]) if row else None


def _authorise_password_change(
    conn: sqlite3.Connection,
    username: str,
    old_password: str,
    new_password: str,
    namespace: str,
    *,
    enforce_policy: bool,
) -> str | None:
    """Run the pre-transaction guard sequence for `change_password`.

    Returns the stored hash *old_password* was checked against when the
    rotation may proceed, None when it is rejected (the reauth namespace
    is locked, or the old password is wrong). Nothing is written to ``admin_users`` on either outcome —
    only *username*'s re-auth failure counter is bumped on a rejection,
    mirroring the escalation behaviour of plain login. A login lockout
    never blocks the check and a failure never adds to it; a right old
    password clears an unlocked login counter, and the re-auth counter
    too where the new password is refused.

    Raises:
        ValueError: *new_password* fails the strength policy — only
            when *enforce_policy* is true. Raised before the caller
            opens its transaction, so nothing is written.
    """
    from mediaman.web.auth.login_lockout import (
        clear_unlocked_login_failures,
        record_failure,
    )

    if namespace and is_locked_out(conn, namespace):
        # Burn a constant-time bcrypt cycle so timing matches the
        # wrong-password path; bump the counter so a sustained attack
        # escalates the lock window.
        bcrypt.checkpw(_prepare_bcrypt_input(old_password), _get_dummy_hash())
        record_failure(conn, namespace)
        logger.warning(
            "password.change_locked user=%s reason=lockout_active",
            _sanitise_log_field(username),
        )
        return None

    # Read before the check: a hash changed in between makes the write refuse, never succeed.
    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    if not verify_password(conn, username, old_password) or row is None:
        if namespace:
            record_failure(conn, namespace)
        logger.warning(
            "password.change_failed user=%s reason=wrong_old_password",
            _sanitise_log_field(username),
        )
        return None
    clear_unlocked_login_failures(conn, username)

    if enforce_policy:
        from mediaman.web.auth.password_policy import password_issues

        issues = password_issues(new_password, username=username)
        if issues:
            if namespace:
                _clear_reauth_failure_counter(conn, namespace, username)
            raise ValueError("Password does not meet strength policy: " + "; ".join(issues))

    return str(row["password_hash"])


def _clear_reauth_failure_counter(conn: sqlite3.Connection, namespace: str, username: str) -> None:
    """Best-effort clear of *username*'s re-auth failure counter.

    Called outside `change_password`'s transaction so a counter
    write failure never blocks a successful rotation. We are already
    past the bcrypt+UPDATE so the worst that happens here is a stale
    1-2 entry sitting around.

    rationale: best-effort failure-counter cleanup — the password has already
    changed; a stale counter entry is cosmetic noise, not a correctness failure.
    Narrowed to ``sqlite3.Error``: ``record_success`` is a single DELETE +
    commit, so a DB error is the only failure worth swallowing here. A
    non-DB exception means a bug in ``record_success`` and must propagate.
    """
    from mediaman.web.auth.login_lockout import record_success

    try:
        record_success(conn, namespace)
    except sqlite3.Error:  # pragma: no cover — counter cleanup is best-effort
        logger.exception(
            "password.change counter cleanup failed user=%s", _sanitise_log_field(username)
        )


# rationale: verify old password, enforce policy, bcrypt the new one, write
# the hash, rotate the session token, and write an audit row — all must happen
# in a single DB transaction so a failure between the hash write and the audit
# write cannot produce an audit-free password change.
def change_password(
    conn: sqlite3.Connection,
    username: str,
    old_password: str,
    new_password: str,
    *,
    enforce_policy: bool = True,
    audit_actor: str | None = None,
    audit_ip: str = "",
    audit_event: str = "password.changed",
) -> bool:
    """Change a user's password.

    Returns True on success, False if the old password is wrong, the
    user no longer exists or their password changed since the check
    (TOCTOU), or the reauth namespace is locked.

    Wrong-old-password attempts are recorded under *username*'s re-auth
    key in `mediaman.web.auth.login_lockout` so a stolen session cannot
    turn this endpoint into an offline-style password oracle — the same
    escalating 5/10/15 thresholds that gate plain login also gate
    ``change_password``. A wrong old password never bumps the plain-login
    counter for *username*, and a login lockout never blocks the change:
    otherwise an attacker with a session cookie could lock the legitimate
    user out of the login flow without ever knowing the password, and an
    attacker hammering the login form could block a live session's
    password change.

    Audit-in-transaction: when *audit_actor* is supplied (typically the
    same as *username*), a ``sec:<audit_event>`` row is written inside
    the same ``BEGIN IMMEDIATE`` that flips the password hash, drops
    sessions, and revokes reauth tickets. If the audit insert fails,
    the entire rotation rolls back — we never have a "the password
    changed but no audit trail exists" outcome.

    TOCTOU: between `verify_password` returning True and the UPDATE
    landing, another worker or process could delete the user or change
    their password (a second change at once, or a reset). The UPDATE
    only lands on the hash that was checked; ``cursor.rowcount`` of zero
    rolls back with ``return False`` rather than silently claim success.
    Such a refusal counts no failure: the old password was right when
    checked, and is no longer the password.

    Reauth ticket cleanup is performed inside the same transaction as
    the session DELETE so a thief who held a reauth ticket cannot
    redeem it under the freshly-issued sessions that follow the
    password change.

    Raises:
        ValueError: *new_password* fails the strength policy — only
            when *enforce_policy* is true. Raised before the
            transaction opens, so nothing is written.
        sqlite3.Error: a ``security_event_or_raise`` audit-write
            failure (or any other DB error inside the ``with conn:``
            block) propagates and rolls the whole rotation back —
            fail-closed, so a password change never lands without its
            audit row.
    """
    namespace = reauth_key(username) if username else ""

    with one_attempt_at_a_time(namespace):
        verified_hash = _authorise_password_change(
            conn,
            username,
            old_password,
            new_password,
            namespace,
            enforce_policy=enforce_policy,
        )
    if verified_hash is None:
        return False

    new_hash = bcrypt.hashpw(
        _prepare_bcrypt_input(new_password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    ).decode()
    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics. Raising the
    # private ``_UserVanished`` sentinel from inside the block triggers
    # the rollback and is caught immediately below — keeping the rollback
    # and the False-return in the same code path.
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE admin_users SET password_hash=?, must_change_password=0"
                " WHERE username=? AND password_hash=?",
                (new_hash, username, verified_hash),
            )
            # TOCTOU guard: if the user vanished or their hash changed between
            # verify_password() and this UPDATE, rowcount will be zero. Roll
            # back instead of claiming success — we just wrote a no-op and
            # would otherwise also DELETE sessions / mint an audit row for a
            # password that is not the one set.
            if cursor.rowcount == 0:
                raise _UserVanished(username)  # triggers with-block rollback
            conn.execute("DELETE FROM admin_sessions WHERE username=?", (username,))
            # Reauth ticket revocation belongs INSIDE the transaction.
            # Otherwise a thief holding a reauth ticket whose session we
            # just dropped could redeem the ticket against a brand-new
            # session that re-authenticates with the same username. We
            # inline the DELETE rather than calling
            # ``revoke_all_reauth_for`` because that helper commits its
            # own transaction, which would prematurely commit the outer
            # block.
            conn.execute(
                "DELETE FROM reauth_tickets WHERE username = ?",
                (username,),
            )
            if audit_actor is not None:
                from mediaman.core.audit import security_event_or_raise

                security_event_or_raise(
                    conn,
                    event=audit_event,
                    actor=audit_actor,
                    ip=audit_ip,
                    detail={"target_username": username},
                )
    except _UserVanished:
        logger.warning(
            "password.change_failed user=%s reason=user_vanished_or_password_changed",
            _sanitise_log_field(username),
        )
        return False
    if namespace:
        _clear_reauth_failure_counter(conn, namespace, username)
    logger.info("password.changed user=%s sessions_revoked=all", _sanitise_log_field(username))
    return True
