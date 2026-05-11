"""Bcrypt password hashing, verification, and rotation.

Split from ``auth/session.py`` (R2). Owns the "how are passwords hashed
and compared" concern; session persistence lives in
:mod:`mediaman.web.auth.session_store`; user CRUD helpers live in
:mod:`mediaman.web.auth.user_crud`.

Bcrypt 72-byte truncation defence
---------------------------------

``bcrypt.hashpw`` silently truncates its input to 72 bytes — two
different 100-byte passwords whose first 72 bytes match would hash to
the same value.  We defeat that by pre-hashing any password that
exceeds 72 bytes (after Unicode NFKC normalisation) with SHA-256 and
base64-encoding the digest (44 bytes — comfortably under bcrypt's
limit).  Inputs at or below 72 bytes bypass the pre-hash so existing
``admin_users`` rows continue to verify unchanged.

Both ``hashpw`` and ``checkpw`` callers MUST go through
:func:`_prepare_bcrypt_input` so the encoding stays symmetric.  A
mismatch (pre-hash on set, raw on verify or vice versa) would lock
every user out.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re as _re
import sqlite3
import threading
import unicodedata

import bcrypt

from mediaman.core.time import now_iso

# Re-export the user-CRUD helpers so callers continue to import them via
# ``mediaman.web.auth.password_hash``.  Splitting these to a sibling
# module keeps ``password_hash`` under the file-size ceiling without
# moving the ``patch("...password_hash.bcrypt")`` target tests rely on.
from mediaman.web.auth.user_crud import (
    UserRecord,
    delete_user,
    list_users,
    set_must_change_password,
    user_must_change_password,
)

logger = logging.getLogger(__name__)


#: Single source of truth for the bcrypt cost factor. Tied together so
#: the dummy hash and every real hash use the same work factor — a
#: drift here would create a measurable timing channel between
#: nonexistent-user and real-user code paths.
BCRYPT_ROUNDS = 12

#: Hard cap on bcrypt input. ``bcrypt.hashpw`` silently truncates above
#: this; we redirect oversized inputs through a SHA-256 pre-hash so the
#: full input contributes to the final digest.
_BCRYPT_MAX_INPUT_BYTES = 72


# Characters allowed in sanitised log fields. Anything else is stripped
# before interpolation so usernames cannot inject CR/LF or control
# characters into log lines. ``web.routes.auth`` imports this as the
# canonical definition — having the regex here (in the auth package)
# rather than in the route module is the correct direction and avoids
# any import cycle.
_LOG_FIELD_RE = _re.compile(r"[^A-Za-z0-9._@\-]")


def _sanitise_log_field(value: str, limit: int = 64) -> str:
    """Strip non-safe characters from *value* and truncate to *limit*."""
    truncated = len(value) > limit
    sanitised = _LOG_FIELD_RE.sub("", value)[:limit]
    return sanitised + "..." if truncated else sanitised


def _normalise_password(password: str) -> str:
    """NFKC-normalise *password* so visually-identical strings agree.

    Different OSes / IMEs emit different byte sequences for the same
    visible character (e.g. ``é`` as precomposed code point vs.
    ``e`` + combining acute).  NFKC folds those representations and
    compatibility forms so the same typed password hashes the same
    everywhere — otherwise a user who set their password on one
    platform could not log in from another.
    """
    return unicodedata.normalize("NFKC", password)


def _prepare_bcrypt_input(password: str) -> bytes:
    """Return the bytes that should be fed into ``bcrypt.hashpw``/``checkpw``.

    Inputs at or below bcrypt's 72-byte limit (after NFKC normalisation)
    pass straight through so existing hashes continue to verify
    unchanged.  Longer inputs are SHA-256 hashed and base64-encoded
    (44 bytes — well under the threshold) so the full entropy of the
    original password reaches the final digest.  Both ``hashpw`` and
    ``checkpw`` MUST route through this helper — a mismatch would
    lock every user out.
    """
    normalised = _normalise_password(password)
    encoded = normalised.encode("utf-8")
    if len(encoded) <= _BCRYPT_MAX_INPUT_BYTES:
        return encoded
    digest = hashlib.sha256(encoded).digest()
    # Stdlib base64 — keep the padding so the encoding is unambiguous.
    return base64.b64encode(digest)


_DUMMY_HASH: bytes | None = None
_DUMMY_HASH_LOCK = threading.Lock()


def _get_dummy_hash() -> bytes:
    """Lazily compute the bcrypt dummy hash the first time it's needed."""
    global _DUMMY_HASH
    with _DUMMY_HASH_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
        return _DUMMY_HASH


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
    through :func:`_prepare_bcrypt_input` first so inputs over 72 bytes
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
            raise ValueError(f"User '{username}' already exists") from exc
        logger.error("create_user integrity_error user=%s detail=%s", username, exc)
        raise


def _short_circuit_for_lockout(
    conn: sqlite3.Connection, username: str, record_failures: bool
) -> bool:
    """Return ``True`` when *username* is currently locked out.

    Records a failure to keep the escalation thresholds reachable (C6).
    Logging on this path uses ``username`` directly because the caller
    only reaches it when ``username`` is non-empty.
    """
    from mediaman.web.auth.login_lockout import is_locked_out, record_failure

    if not is_locked_out(conn, username):
        return False
    if record_failures:
        record_failure(conn, username)
    logger.warning("auth.account_locked user=%s reason=lockout_active", username)
    return True


def _verify_against_dummy_hash(password: str) -> None:
    """Burn one constant-time bcrypt cycle for nonexistent-user probes.

    Equalises wall-time between real-username and fake-username probes
    so an attacker cannot enumerate valid usernames via timing.  The
    return value is intentionally discarded.
    """
    bcrypt.checkpw(_prepare_bcrypt_input(password), _get_dummy_hash())


def _record_login_outcome(
    conn: sqlite3.Connection, username: str, ok: bool, record_failures: bool
) -> None:
    """Persist the success/failure outcome of an authentication attempt."""
    from mediaman.web.auth.login_lockout import record_failure, record_success

    if ok:
        record_success(conn, username)
    elif record_failures:
        record_failure(conn, username)


def authenticate(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    record_failures: bool = True,
) -> bool:
    """Verify username/password credentials.

    Always performs a bcrypt check — even for nonexistent users — to
    prevent timing-based username enumeration.  Two short-circuit
    paths skip the bcrypt cycle deliberately: empty usernames (CPU-DoS
    defence) and already-locked accounts (see C6 in
    ``test_login_lockout.py`` and the M21 escalation property in
    :mod:`mediaman.web.auth.login_lockout`).  The "constant-time"
    property is preserved across the meaningful branches: a real-name
    probe and a fake-name probe both pay one bcrypt round.
    """
    # Empty username and locked accounts skip bcrypt deliberately — see
    # the docstring for the CPU-DoS / escalation-counter trade-offs.
    if not username:
        return False
    if _short_circuit_for_lockout(conn, username, record_failures):
        return False

    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    ).fetchone()
    if row is None:
        # Burn a constant-time bcrypt cycle so a real-username probe and
        # a fake-username probe take ~the same wall time and timing
        # cannot enumerate valid usernames.
        _verify_against_dummy_hash(password)
        _record_login_outcome(conn, username, ok=False, record_failures=record_failures)
        return False

    ok = bcrypt.checkpw(_prepare_bcrypt_input(password), row["password_hash"].encode())
    _record_login_outcome(conn, username, ok=ok, record_failures=record_failures)
    return ok


def _reauth_namespace(username: str) -> str:
    """Return the reauth-lockout namespace for *username* (or empty string)."""
    from mediaman.web.auth.reauth import REAUTH_LOCKOUT_PREFIX

    return f"{REAUTH_LOCKOUT_PREFIX}{username}" if username else ""


def _check_reauth_lockout(
    conn: sqlite3.Connection, username: str, namespace: str, old_password: str
) -> bool:
    """Return ``True`` when *namespace* is locked out for reauth.

    Burns a constant-time bcrypt cycle so timing matches the
    wrong-password path and bumps the namespace counter so a sustained
    attack escalates the lock window.
    """
    from mediaman.web.auth.login_lockout import is_locked_out, record_failure

    if not namespace or not is_locked_out(conn, namespace):
        return False
    bcrypt.checkpw(_prepare_bcrypt_input(old_password), _get_dummy_hash())
    record_failure(conn, namespace)
    logger.warning(
        "password.change_locked user=%s reason=lockout_active",
        _sanitise_log_field(username),
    )
    return True


def _verify_old_password(
    conn: sqlite3.Connection, username: str, namespace: str, old_password: str
) -> bool:
    """Return ``True`` when *old_password* matches the stored hash.

    Records a failure on the reauth namespace on mismatch so a stolen
    session cannot turn this endpoint into an offline password oracle.
    """
    from mediaman.web.auth.login_lockout import record_failure

    if authenticate(conn, username, old_password, record_failures=False):
        return True
    if namespace:
        record_failure(conn, namespace)
    logger.warning(
        "password.change_failed user=%s reason=wrong_old_password",
        _sanitise_log_field(username),
    )
    return False


def _hash_new_password(new_password: str, username: str, *, enforce_policy: bool) -> str:
    """Run the policy check and return the bcrypt-encoded new password hash.

    Raises :exc:`ValueError` when *enforce_policy* is true and *new_password*
    fails the strength policy — surfaced to callers as a 400-style error
    before any DB write happens.
    """
    if enforce_policy:
        from mediaman.web.auth.password_policy import password_issues

        issues = password_issues(new_password, username=username)
        if issues:
            raise ValueError("Password does not meet strength policy: " + "; ".join(issues))

    return bcrypt.hashpw(
        _prepare_bcrypt_input(new_password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    ).decode()


def _write_rotation_audit_row(
    conn: sqlite3.Connection,
    *,
    audit_actor: str,
    audit_ip: str,
    audit_event: str,
    target_username: str,
) -> None:
    """Insert the security audit row for a successful password rotation."""
    from mediaman.core.audit import security_event_or_raise

    security_event_or_raise(
        conn,
        event=audit_event,
        actor=audit_actor,
        ip=audit_ip,
        detail={"target_username": target_username},
    )


def _persist_password_rotation(
    conn: sqlite3.Connection,
    username: str,
    new_hash: str,
    *,
    audit_actor: str | None,
    audit_ip: str,
    audit_event: str,
) -> bool:
    """Apply the password update + session/reauth wipe + audit row atomically.

    Returns ``True`` on success, ``False`` when the TOCTOU guard fires
    (the user row vanished between authenticate and the UPDATE).
    """
    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics. The
    # _user_vanished flag lets us return False AFTER the with-block
    # has rolled back via the sentinel raise.
    _user_vanished = False
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE admin_users SET password_hash=?, must_change_password=0 WHERE username=?",
                (new_hash, username),
            )
            # TOCTOU guard: if the user vanished between authenticate() and
            # this UPDATE, rowcount will be zero. Roll back instead of
            # claiming success.
            if cursor.rowcount == 0:
                _user_vanished = True
                raise RuntimeError("user_vanished")  # triggers with-block rollback
            conn.execute("DELETE FROM admin_sessions WHERE username=?", (username,))
            # Reauth ticket revocation belongs INSIDE the transaction so a
            # thief holding a reauth ticket cannot redeem it against a
            # brand-new session re-authenticated with the same username.
            # ``revoke_all_reauth_for`` commits its own transaction so we
            # inline the DELETE instead.
            conn.execute("DELETE FROM reauth_tickets WHERE username = ?", (username,))
            if audit_actor is not None:
                _write_rotation_audit_row(
                    conn,
                    audit_actor=audit_actor,
                    audit_ip=audit_ip,
                    audit_event=audit_event,
                    target_username=username,
                )
    except RuntimeError:
        if _user_vanished:
            logger.warning(
                "password.change_failed user=%s reason=user_vanished",
                _sanitise_log_field(username),
            )
            return False
        raise
    return True


def _clear_reauth_counter(conn: sqlite3.Connection, namespace: str, username: str) -> None:
    """Best-effort clear of the reauth failure counter after a successful rotation.

    Done outside the transaction so a counter-write hiccup never blocks
    a successful password change — the worst case is a stale 1-2 entry
    sitting around.
    """
    if not namespace:
        return
    from mediaman.web.auth.login_lockout import record_success

    try:
        record_success(conn, namespace)
    except Exception:  # pragma: no cover — counter cleanup is best-effort
        logger.exception("password.change counter cleanup failed user=%s", username)


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

    Wrong-old-password attempts are recorded against the
    ``reauth:<username>`` namespace of :mod:`mediaman.web.auth.login_lockout`
    so a stolen session cannot use this endpoint as an offline password
    oracle.  The plain-login counter for *username* is intentionally
    left untouched so a session-holder cannot lock the legitimate user
    out of the login flow.

    The hash update, session wipe, reauth-ticket revocation, and audit
    row all run inside one ``BEGIN IMMEDIATE`` — see
    :func:`_persist_password_rotation` for the rationale (TOCTOU guard,
    intra-transaction ticket revocation, audit-in-transaction).
    """
    namespace = _reauth_namespace(username)

    if _check_reauth_lockout(conn, username, namespace, old_password):
        return False

    if not _verify_old_password(conn, username, namespace, old_password):
        return False

    new_hash = _hash_new_password(new_password, username, enforce_policy=enforce_policy)

    if not _persist_password_rotation(
        conn,
        username,
        new_hash,
        audit_actor=audit_actor,
        audit_ip=audit_ip,
        audit_event=audit_event,
    ):
        return False

    _clear_reauth_counter(conn, namespace, username)
    logger.info("password.changed user=%s sessions_revoked=all", username)
    return True


# Public API surface — ``__all__`` documents the back-compat re-export
# of CRUD helpers from :mod:`mediaman.web.auth.user_crud` so external
# imports continue to resolve through this module path.
__all__ = [
    "BCRYPT_ROUNDS",
    "UserRecord",
    "authenticate",
    "change_password",
    "create_user",
    "delete_user",
    "list_users",
    "set_must_change_password",
    "user_must_change_password",
]
