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
hashed before this change continue to verify: any password short enough
for bcrypt to accept directly (≤ 72 bytes) bypasses the pre-hash on
both the set and verify paths and hits the same bytes the original
``hashpw`` did. No backfill or lazy migration is required — the only
behaviour change is for pathological inputs over 72 bytes, which
nobody could ever have logged in with reliably anyway.

Both ``hashpw`` and ``checkpw`` callers MUST go through
:func:`_prepare_bcrypt_input` so the pre-hash logic is applied
symmetrically. A mismatch (pre-hash on set, raw on verify or vice
versa) would lock everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re as _re
import sqlite3
import threading
import unicodedata
from typing import TypedDict

import bcrypt

from mediaman.core.time import now_iso

logger = logging.getLogger("mediaman")


class UserRecord(TypedDict):
    """A single admin user row returned by :func:`list_users`."""

    id: int
    username: str
    created_at: str


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
    if value is None:
        return ""
    truncated = len(value) > limit
    sanitised = _LOG_FIELD_RE.sub("", value)[:limit]
    return sanitised + "..." if truncated else sanitised


def _normalise_password(password: str) -> str:
    """NFKC-normalise *password* so visually-identical strings agree.

    Different OSes / IMEs emit different byte sequences for the same
    visible character — e.g. ``é`` may arrive as a single precomposed
    code point or as ``e`` + combining acute. Without normalisation,
    those two strings hash to different bcrypt outputs, so a user who
    set their password on one platform cannot log in from another.
    NFKC also folds compatibility forms (full-width digits, ligatures,
    etc.) so the normalised representation is stable across input
    methods.
    """
    return unicodedata.normalize("NFKC", password)


def _prepare_bcrypt_input(password: str) -> bytes:
    """Return the bytes that should be fed into ``bcrypt.hashpw``/``checkpw``.

    Inputs that fit within bcrypt's 72-byte limit (after NFKC
    normalisation) are passed straight through, so existing hashes that
    were generated before this defence landed continue to verify
    against the same bytes. Inputs that exceed the limit are pre-hashed
    with SHA-256 and base64-encoded — the result is 44 bytes, well
    under bcrypt's threshold, so the full entropy of the original
    password reaches the final digest.

    Both ``hashpw`` and ``checkpw`` MUST route through this helper so
    the encoding stays symmetric. A mismatch would lock every user out.
    """
    normalised = _normalise_password(password)
    encoded = normalised.encode("utf-8")
    if len(encoded) <= _BCRYPT_MAX_INPUT_BYTES:
        return encoded
    digest = hashlib.sha256(encoded).digest()
    # base64 of a 32-byte digest is 44 chars — comfortably under bcrypt's
    # 72-byte limit. We keep the b64 padding (rather than stripping it)
    # so the encoding is exactly what stdlib base64 produces, leaving
    # zero ambiguity when re-deriving on verify.
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
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at, must_change_password) "
            "VALUES (?, ?, ?, 0)",
            (username, password_hash, now),
        )
        if audit_actor is not None:
            from mediaman.audit import security_event_or_raise

            security_event_or_raise(
                conn,
                event="user.created",
                actor=audit_actor,
                ip=audit_ip,
                detail={"new_username": username},
            )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        message = (exc.args[0] if exc.args else "").lower()
        if "unique" in message and "admin_users.username" in message:
            raise ValueError(f"User '{username}' already exists") from exc
        logger.error("create_user integrity_error user=%s detail=%s", username, exc)
        raise
    except Exception:
        conn.execute("ROLLBACK")
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
    from mediaman.web.auth.login_lockout import (
        check_lockout,
        record_failure,
        record_success,
    )

    # Reject empty usernames before touching bcrypt. Otherwise an
    # unauthenticated attacker can stream empty-username requests at
    # the login endpoint and burn server CPU at one bcrypt round per
    # request — a cheap CPU-DoS.
    if not username:
        return False

    # Check lockout first. A locked account already has a "no" answer
    # without re-running bcrypt — skip the dummy round and save the
    # CPU. We still record the failure so the escalation thresholds
    # remain reachable (5 → 10 → 15 failures); see C6 in the lockout
    # tests. record_failure keeps acquiring the writer lock, which is
    # the price of the escalation property.
    if check_lockout(conn, username):
        if record_failures:
            record_failure(conn, username)
        logger.warning("auth.account_locked user=%s reason=lockout_active", username)
        return False

    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    ).fetchone()

    if row is None:
        # Burn a constant-time bcrypt cycle so a real-username probe and
        # a fake-username probe take ~the same wall time and timing
        # cannot enumerate valid usernames.
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
        check_lockout,
        record_failure,
        record_success,
    )
    from mediaman.web.auth.reauth import REAUTH_LOCKOUT_PREFIX

    namespace = f"{REAUTH_LOCKOUT_PREFIX}{username}" if username else ""

    if namespace and check_lockout(conn, namespace):
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
    conn.execute("BEGIN IMMEDIATE")
    try:
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
            conn.execute("ROLLBACK")
            logger.warning(
                "password.change_failed user=%s reason=user_vanished",
                _sanitise_log_field(username),
            )
            return False
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
            from mediaman.audit import security_event_or_raise

            security_event_or_raise(
                conn,
                event=audit_event,
                actor=audit_actor,
                ip=audit_ip,
                detail={"target_username": username},
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if namespace:
        # Clear the failure counter outside the transaction so a counter
        # write failure never blocks a successful rotation. We are
        # already past the bcrypt+UPDATE so the worst that happens here
        # is a stale 1-2 entry sitting around.
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

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM admin_sessions WHERE username=?", (target_username,))
        cursor = conn.execute(
            "DELETE FROM admin_users WHERE id = ? AND (SELECT COUNT(*) FROM admin_users) > 1",
            (user_id,),
        )
        if cursor.rowcount == 0:
            conn.execute("ROLLBACK")
            return False
        if audit_actor is not None:
            from mediaman.audit import security_event_or_raise

            security_event_or_raise(
                conn,
                event="user.deleted",
                actor=audit_actor,
                ip=audit_ip,
                detail={"target_id": user_id, "target_username": target_username},
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    # Best-effort cleanup of any reauth tickets the deleted user held —
    # done outside the transaction so a tickets-table hiccup never
    # blocks a successful delete.
    try:
        from mediaman.web.auth.reauth import revoke_all_reauth_for

        revoke_all_reauth_for(conn, target_username)
    except Exception:  # pragma: no cover — never break flow on cleanup failure
        logger.exception("delete_user reauth cleanup failed user=%s", target_username)
    return True
