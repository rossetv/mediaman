"""Password rotation — :func:`change_password` and its guards.

Splits the rotation into three parts so the ``with conn:`` block in
:func:`change_password` stands alone and its "all these writes share
one transaction" rationale is literally true of what remains:

* :func:`_authorise_password_change` — the pre-transaction guard
  sequence (reauth-lockout check, old-password verification, new-
  password policy). Runs before anything is written.
* :func:`change_password` — the transactional UPDATE + session purge +
  reauth-ticket purge + audit, plus TOCTOU handling.
* :func:`_clear_reauth_failure_counter` — the best-effort post-
  transaction failure-counter cleanup.

User CRUD lives in :mod:`._user_crud`; the credential-verification
path in :mod:`._authenticate`.
"""

from __future__ import annotations

import logging
import sqlite3

import bcrypt

from mediaman.web.auth._password_hash_helpers import (
    BCRYPT_ROUNDS,
    _get_dummy_hash,
    _prepare_bcrypt_input,
    _sanitise_log_field,
    _UserVanished,
)
from mediaman.web.auth.password_hash._authenticate import authenticate

logger = logging.getLogger(__name__)


def _authorise_password_change(
    conn: sqlite3.Connection,
    username: str,
    old_password: str,
    new_password: str,
    namespace: str,
    *,
    enforce_policy: bool,
) -> bool:
    """Run the pre-transaction guard sequence for :func:`change_password`.

    Returns True when the rotation may proceed, False when it is
    rejected (the reauth namespace is locked, or the old password is
    wrong). Nothing is written to ``admin_users`` on either outcome —
    only the ``reauth:<username>`` failure counter is bumped on a
    rejection, mirroring the escalation behaviour of plain login.

    Raises:
        ValueError: *new_password* fails the strength policy — only
            when *enforce_policy* is true. Raised before the caller
            opens its transaction, so nothing is written.
    """
    from mediaman.web.auth.login_lockout import is_locked_out, record_failure

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
        return False

    if not authenticate(conn, username, old_password, record_failures=False):
        if namespace:
            record_failure(conn, namespace)
        logger.warning(
            "password.change_failed user=%s reason=wrong_old_password",
            _sanitise_log_field(username),
        )
        return False

    if enforce_policy:
        from mediaman.web.auth.password_policy import password_issues

        issues = password_issues(new_password, username=username)
        if issues:
            raise ValueError("Password does not meet strength policy: " + "; ".join(issues))

    return True


def _clear_reauth_failure_counter(conn: sqlite3.Connection, namespace: str, username: str) -> None:
    """Best-effort clear of the ``reauth:<username>`` failure counter.

    Called outside :func:`change_password`'s transaction so a counter
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
        logger.exception("password.change counter cleanup failed user=%s", username)


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
    user no longer exists (TOCTOU), or the reauth namespace is locked.

    Wrong-old-password attempts are recorded into the
    ``reauth:<username>`` namespace of :mod:`mediaman.web.auth.login_lockout`
    so a stolen session cannot turn this endpoint into an offline-style
    password oracle — the same escalating 5/10/15 thresholds that gate
    plain login also gate ``change_password``. The plain-login counter
    for *username* is intentionally left untouched: otherwise an
    attacker with a session cookie could lock the legitimate user out
    of the login flow without ever knowing the password.

    Audit-in-transaction: when *audit_actor* is supplied (typically the
    same as *username*), a ``sec:<audit_event>`` row is written inside
    the same ``BEGIN IMMEDIATE`` that flips the password hash, drops
    sessions, and revokes reauth tickets. If the audit insert fails,
    the entire rotation rolls back — we never have a "the password
    changed but no audit trail exists" outcome.

    TOCTOU: between :func:`authenticate` returning True and the UPDATE
    landing, the user could be deleted by another worker. We detect
    that via ``cursor.rowcount`` and roll back with ``return False``
    rather than silently claim success.

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
    # Lazy import: ``reauth`` imports ``authenticate`` from this package,
    # so a module-level import here would close the cycle. The original
    # ``change_password`` imported ``REAUTH_LOCKOUT_PREFIX`` inside its
    # body for exactly this reason.
    from mediaman.web.auth.reauth import REAUTH_LOCKOUT_PREFIX

    namespace = f"{REAUTH_LOCKOUT_PREFIX}{username}" if username else ""

    if not _authorise_password_change(
        conn,
        username,
        old_password,
        new_password,
        namespace,
        enforce_policy=enforce_policy,
    ):
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
                "UPDATE admin_users SET password_hash=?, must_change_password=0 WHERE username=?",
                (new_hash, username),
            )
            # TOCTOU guard: if the user vanished between authenticate() and
            # this UPDATE, rowcount will be zero. Roll back instead of
            # claiming success — we just wrote a no-op and would otherwise
            # also DELETE sessions / mint an audit row for a user who is no
            # longer there.
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
            "password.change_failed user=%s reason=user_vanished",
            _sanitise_log_field(username),
        )
        return False
    if namespace:
        _clear_reauth_failure_counter(conn, namespace, username)
    logger.info("password.changed user=%s sessions_revoked=all", username)
    return True
