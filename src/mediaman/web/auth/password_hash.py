"""Bcrypt password hashing, verification, and rotation.

Split from ``auth/session.py`` (R2). Owns the "how are passwords hashed
and compared" concern; session persistence lives in
:mod:`mediaman.web.auth.session_store`.

Bcrypt 72-byte truncation defence
---------------------------------

``bcrypt.hashpw`` silently truncates its input to 72 bytes — two
different 100-byte passwords whose first 72 bytes match would hash to
the same value, and an attacker who knows this can craft pathological
inputs. We defeat that by pre-hashing any password that exceeds 72
bytes (after Unicode NFKC normalisation) with SHA-256, base64-encoding
the digest (44 bytes — comfortably under bcrypt's limit) and feeding
the result into bcrypt. Two distinct long passwords therefore have
distinct SHA-256 digests and bcrypt sees full-entropy inputs.

The gate is keyed on input length so existing ``admin_users`` rows
hashed before this change landed continue to verify: any password short
enough for bcrypt to accept directly (≤ 72 bytes) bypasses the pre-hash
on both the set and verify paths and hits the same bytes the original
``hashpw`` did. No backfill or lazy migration is required — the only
behaviour change is for pathological inputs over 72 bytes, which
nobody could ever have logged in with reliably anyway.

The bcrypt-input preparation pipeline (NFKC normalise → SHA-256 +
base64 for oversize), the cached dummy hash, the rollback sentinels,
and the log-field sanitiser live in
:mod:`mediaman.web.auth._password_hash_helpers`. This file owns the
user CRUD operations on top of those helpers.

Both ``hashpw`` and ``checkpw`` callers MUST go through
``_prepare_bcrypt_input`` so the pre-hash logic is applied
symmetrically. A mismatch (pre-hash on set, raw on verify or vice
versa) would lock everyone out.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TypedDict

import bcrypt

from mediaman.core.time import now_iso
from mediaman.web.auth._password_hash_helpers import (
    BCRYPT_ROUNDS,
    _get_dummy_hash,
    _LastUser,
    _prepare_bcrypt_input,
    _sanitise_log_field,
    _UserVanished,
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
    preserve full entropy (see module docstring).

    Audit-in-transaction: when *audit_actor* is supplied, a
    ``sec:user.created`` row is written inside the same
    ``BEGIN IMMEDIATE`` that inserts the user. If the audit insert
    blows up, the user-creation rolls back — we never have a "user
    minted but no audit trail exists" outcome.
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


def _short_circuit_authenticate(
    conn: sqlite3.Connection,
    username: str,
    *,
    record_failures: bool,
) -> bool | None:
    """Return a definite answer for the no-bcrypt fast paths.

    Returns ``False`` when the request is rejected without bcrypt
    (empty username, or locked account). Returns ``None`` when the
    caller must continue with the bcrypt verification path.

    The locked branch still bumps :func:`record_failure` so the
    escalation thresholds (5 → 10 → 15 failures → 15 min / 1 h / 24 h)
    remain reachable while the lock window is active — see C6 in
    ``test_login_lockout.py``.
    """
    from mediaman.web.auth.login_lockout import is_locked_out, record_failure

    # Reject empty usernames before touching bcrypt. Otherwise an
    # unauthenticated attacker can stream empty-username requests at
    # the login endpoint and burn server CPU at one bcrypt round per
    # request — a cheap CPU-DoS.
    if not username:
        return False

    # Check lockout first. A locked account already has a "no" answer
    # without re-running bcrypt — skip the dummy round and save the
    # CPU. record_failure keeps acquiring the writer lock, which is
    # the price of the escalation property.
    if is_locked_out(conn, username):
        if record_failures:
            record_failure(conn, username)
        logger.warning("auth.account_locked user=%s reason=lockout_active", username)
        return False

    return None


def _verify_credentials_against_db(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    record_failures: bool,
) -> bool:
    """Do the bcrypt-verify dance against the stored hash for *username*.

    Burns a constant-time dummy bcrypt cycle on the "user not found"
    path so a real-username probe and a fake-username probe take ~the
    same wall time — timing cannot enumerate valid usernames.
    """
    from mediaman.web.auth.login_lockout import record_failure, record_success

    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    ).fetchone()

    if row is None:
        bcrypt.checkpw(_prepare_bcrypt_input(password), _get_dummy_hash())
        if record_failures:
            record_failure(conn, username)
        return False

    ok = bcrypt.checkpw(_prepare_bcrypt_input(password), row["password_hash"].encode())
    if ok:
        record_success(conn, username)
    elif record_failures:
        record_failure(conn, username)
    return ok


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

    Two short-circuit paths skip the bcrypt cycle deliberately:

    * Empty username — there is no user to authenticate, and burning a
      bcrypt round per request would let an unauthenticated attacker
      DoS the server's CPU by streaming empty-username login attempts.
    * Account already locked — :mod:`mediaman.web.auth.login_lockout`
      already knows the answer is "no" without re-checking the hash.
      Skipping bcrypt here cuts the cost of a sustained brute-force
      hammering a locked account from one bcrypt round per attempt
      to zero. The ``record_failure`` writer-lock acquisition is
      retained even on the locked path because the escalation
      thresholds (5 → 10 → 15 failures → 15 min / 1 h / 24 h) are
      reachable only while the counter keeps climbing during the
      lock window — see the C6 test in ``test_login_lockout.py``.
      Without the continued counter bump the M21 mitigation in
      :mod:`mediaman.web.auth.login_lockout` cannot escalate to the 1-hour
      and 24-hour windows.

    The "constant-time" property is preserved across the *meaningful*
    branches: an attacker sending a real username gets the same
    bcrypt-cycle latency whether the user exists or not, since the
    "user not found" path still burns a dummy bcrypt round. The empty
    and locked paths are a different (and visible) latency, but the
    information they leak — "you sent no username" or "this account is
    rate-limited" — is information the attacker already controls or
    can deduce from response volume anyway.
    """
    short_circuit = _short_circuit_authenticate(conn, username, record_failures=record_failures)
    if short_circuit is not None:
        return short_circuit
    return _verify_credentials_against_db(conn, username, password, record_failures=record_failures)


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
    """
    from mediaman.web.auth.login_lockout import (
        is_locked_out,
        record_failure,
        record_success,
    )
    from mediaman.web.auth.reauth import REAUTH_LOCKOUT_PREFIX

    namespace = f"{REAUTH_LOCKOUT_PREFIX}{username}" if username else ""

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
        # Clear the failure counter outside the transaction so a counter
        # write failure never blocks a successful rotation. We are
        # already past the bcrypt+UPDATE so the worst that happens here
        # is a stale 1-2 entry sitting around.
        # rationale: best-effort failure-counter cleanup — the password has already
        # changed; a stale counter entry is cosmetic noise, not a correctness failure.
        try:
            record_success(conn, namespace)
        except Exception:  # pragma: no cover — counter cleanup is best-effort
            logger.exception("password.change counter cleanup failed user=%s", username)
    logger.info("password.changed user=%s sessions_revoked=all", username)
    return True


def list_users(conn: sqlite3.Connection) -> list[UserRecord]:
    """Return all admin users (without password hashes)."""
    rows = conn.execute("SELECT id, username, created_at FROM admin_users ORDER BY id").fetchall()
    return [
        {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}
        for row in rows
    ]


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
    try:
        from mediaman.web.auth.reauth import revoke_all_reauth_for

        revoke_all_reauth_for(conn, target_username)
    except Exception:  # pragma: no cover — never break flow on cleanup failure
        logger.exception("delete_user reauth cleanup failed user=%s", target_username)
    return True
