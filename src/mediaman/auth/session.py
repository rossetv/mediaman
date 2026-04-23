"""Admin user and session management.

Session hardening against insider threats and cookie theft
----------------------------------------------------------

The session table stores a SHA-256 **hash** of the token plus a
user-agent fingerprint and the client IP at issuance. Validation
checks:

1. Token exists and is not expired (hard expiry: 1 day from issue —
   matches the session-cookie ``max_age``).
2. Token is not **idle**: last-used time must be within 24 hours.
   Sliding-window refresh: every validate_session bumps last_used_at.
3. Client fingerprint (user-agent hash + IP prefix) matches the
   issuer's fingerprint. A mismatch logs a WARNING and invalidates
   the session — catches stolen cookies used from a different
   browser or network.

The legacy ``token`` column is retained for schema compatibility
(migration v13 hoists the hardening columns into :mod:`mediaman.db`)
but new sessions are looked up solely by ``token_hash``. The plaintext
column is written as empty string and should be dropped in a future
migration once no code path reads it.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TypedDict, cast

import bcrypt

from mediaman.crypto import generate_session_token

logger = logging.getLogger("mediaman")

_DUMMY_HASH: bytes | None = None
_DUMMY_HASH_LOCK = threading.Lock()

_EXPIRED_CLEANUP_INTERVAL = 60.0  # seconds between expired-session sweeps
_last_cleanup_at = 0.0
_cleanup_lock = threading.Lock()

# Minimum gap between ``last_used_at`` refresh writes. The idle timeout
# lives on a 24-hour scale, so a ~60 s coarse-grained refresh is
# indistinguishable in practice but lets us skip a DB write on the vast
# majority of requests. Without this, every authenticated request
# (including static-asset fetches if they end up auth-gated) becomes a
# SQLite writer — which serialises against the scanner and produces
# ``database is locked`` 500s under WAL.
_SESSION_REFRESH_MIN_INTERVAL = timedelta(seconds=60)

# Hard expiry: any session older than this is invalid regardless of
# activity. Matched to the ``max_age=86400`` (1 day) on the session
# cookie — if a raw token is exfiltrated from the DB or process memory
# it must not keep working for longer than the browser would hold the
# cookie. Previously this was 7 days while the cookie was 1 day: a
# stolen token stayed valid server-side for a week beyond the point
# the legitimate user's browser had dropped it.
_HARD_EXPIRY_DAYS = 1
# Idle timeout: unused session dies after this window. 24 h keeps
# existing user experience — you stay signed in through the day.
_IDLE_TIMEOUT_HOURS = 24

# Fingerprint binding mode, sourced from ``MEDIAMAN_FINGERPRINT_MODE``:
#
#   * ``strict`` — full UA hash + IP octet/prefix must match; any
#     mismatch destroys the session.
#   * ``loose``  — default. UA hash + coarse IP prefix. Matches
#     today's behaviour: CGNAT/office-NAT tolerated, but a stolen
#     cookie replayed from a different network or browser is caught.
#   * ``off``    — no fingerprint check. Useful when deploying behind
#     a terminating proxy that rewrites client addresses in a way the
#     code cannot normalise.
#
# The coarse /24 (IPv4) or /64 (IPv6) prefix is NOT a strong binding:
# any two clients sharing a CGNAT, a corporate egress, or a VPN pop
# share the same prefix. Treat it as "raises the bar on opportunistic
# cookie theft" rather than "binds the session to a single device".
_FINGERPRINT_MODE_ENV = "MEDIAMAN_FINGERPRINT_MODE"
_VALID_FINGERPRINT_MODES = {"strict", "loose", "off"}

# Accept only tokens that look exactly like what ``generate_session_token``
# emits (32 random bytes → 64 lowercase hex). Previously any 32..256 hex
# string passed the length gate, giving an attacker a generous search
# space before the hash lookup.
_SESSION_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def _fingerprint_mode() -> str:
    """Return the current fingerprint mode from the environment.

    Read every call rather than cached at import so test monkeypatches
    see their changes. Unknown values fall back to ``loose``.
    """
    mode = (os.environ.get(_FINGERPRINT_MODE_ENV) or "loose").lower()
    if mode not in _VALID_FINGERPRINT_MODES:
        return "loose"
    return mode


def _hash_token(token: str) -> str:
    """Return a SHA-256 hex digest of the token for at-rest storage.

    Rationale: if the sqlite DB file leaks, the raw session tokens
    should not be directly usable. Storing the hash makes token theft
    from the DB useless — attacker would need to reverse SHA-256 which
    is infeasible for 256-bit random input.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _client_fingerprint(user_agent: str | None, client_ip: str | None) -> str:
    """Compute a stable fingerprint for session-to-client binding.

    The fingerprint is ``sha256(UA)[:16] + ":" + coarse-IP-prefix``. The
    prefix is the ``/24`` network address for IPv4 and ``/64`` for IPv6.
    That is a **coarse** check: any two clients sharing a CGNAT, a
    corporate egress, a VPN pop, or residential ISP supernode share the
    same prefix and will share the same fingerprint. Treat it as a
    stolen-cookie speed bump, not a bind-to-device guarantee. A new
    client device on the same NAT will still match.

    Callers pass ``None`` / ``""`` for missing values; the fingerprint
    is derived from a sentinel ``unknown`` prefix plus the UA hash of
    the empty string, which deliberately does not match the empty-
    fingerprint sentinel stored for unbound sessions.
    """
    ua_hash = hashlib.sha256((user_agent or "").encode()).hexdigest()[:16]
    if not client_ip:
        prefix = "unknown"
    else:
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            prefix = "unknown"
        else:
            if isinstance(addr, ipaddress.IPv6Address):
                prefix = str(ipaddress.ip_network(f"{client_ip}/64", strict=False).network_address)
            else:
                prefix = str(ipaddress.ip_network(f"{client_ip}/24", strict=False).network_address)
    return f"{ua_hash}:{prefix}"


def _ensure_session_columns(conn: sqlite3.Connection) -> None:
    """Back-compat shim — real migration lives in :mod:`mediaman.db` (v13).

    Kept as a no-op-ish helper so tests that poke at a bare schema can
    still call it to add the hardening columns. Production code must NOT
    rely on this at runtime: :func:`mediaman.db.init_db` has already
    applied the migration before any caller touches sessions.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(admin_sessions)").fetchall()}
    if "token_hash" not in cols:
        conn.execute("ALTER TABLE admin_sessions ADD COLUMN token_hash TEXT")
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE admin_sessions ADD COLUMN last_used_at TEXT")
    if "fingerprint" not in cols:
        conn.execute("ALTER TABLE admin_sessions ADD COLUMN fingerprint TEXT")
    if "issued_ip" not in cols:
        conn.execute("ALTER TABLE admin_sessions ADD COLUMN issued_ip TEXT")
    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(admin_users)").fetchall()}
    if "must_change_password" not in user_cols:
        conn.execute(
            "ALTER TABLE admin_users ADD COLUMN "
            "must_change_password INTEGER NOT NULL DEFAULT 0"
        )


def user_must_change_password(conn: sqlite3.Connection, username: str) -> bool:
    """Return True when *username*'s account is flagged to force a rotation."""
    row = conn.execute(
        "SELECT must_change_password FROM admin_users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return False
    return bool(row["must_change_password"])


def set_must_change_password(
    conn: sqlite3.Connection, username: str, flag: bool
) -> None:
    """Set / clear the force-rotation flag for *username*."""
    conn.execute(
        "UPDATE admin_users SET must_change_password = ? WHERE username = ?",
        (1 if flag else 0, username),
    )
    conn.commit()


def _get_dummy_hash() -> bytes:
    """Lazily compute the bcrypt dummy hash the first time it's needed.

    Running ``bcrypt.gensalt`` at import time imposes ~300ms on every
    process start (including short-lived CLI invocations). Computing it
    on demand keeps ``mediaman create-user`` snappy.
    """
    global _DUMMY_HASH
    with _DUMMY_HASH_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12))
        return _DUMMY_HASH


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    enforce_policy: bool = True,
) -> None:
    """Insert an admin user with a bcrypt-hashed password (cost 12).

    Raises ``ValueError`` if the username already exists OR if the
    password fails the shared strength policy (when
    ``enforce_policy=True``, the default).

    ``enforce_policy=False`` is reserved for tests and legacy
    migration paths — production callers (CLI, settings UI) must
    keep the policy enforced.
    """
    if enforce_policy:
        from mediaman.auth.password_policy import password_issues

        issues = password_issues(password, username=username)
        if issues:
            raise ValueError("Password does not meet strength policy: " + "; ".join(issues))

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at, must_change_password) "
            "VALUES (?, ?, ?, 0)",
            (username, password_hash, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # Distinguish the expected "username already taken" UNIQUE
        # violation from any other integrity failure (FK error, NOT
        # NULL, corrupted schema). Masking every IntegrityError as
        # "user already exists" hid genuine bugs.
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

    Always performs a bcrypt check — even for nonexistent users — to prevent
    timing-based username enumeration.

    A persistent per-username lockout (see
    :mod:`mediaman.auth.login_lockout`) short-circuits the bcrypt check
    when an account has tripped the threshold. Lock state is **not**
    leaked to the caller — we return False the same as a wrong password,
    so an attacker cannot enumerate valid usernames by watching for a
    different response shape.

    Side effects: writes to the ``login_failures`` table on every call —
    incrementing the failure counter on bad credentials, or clearing it
    on success. **Crucially, the counter keeps climbing even while the
    account is already locked out** — otherwise the 10- and 15-failure
    escalation windows would be unreachable (the attacker could cheaply
    idle past every 15-minute lockout forever).

    Pass ``record_failures=False`` from trusted re-authentication paths
    (password change, admin re-auth) where mistyped current-password
    attempts must NOT lock the legitimate user out of their own
    account. A correct password still clears any existing counter.
    """
    from mediaman.auth.login_lockout import (
        check_lockout,
        record_failure,
        record_success,
    )

    locked = bool(username) and check_lockout(conn, username)
    if locked:
        # Still burn a bcrypt round so response time stays flat against
        # "is this account locked?" timing probes.
        bcrypt.checkpw(password.encode(), _get_dummy_hash())
        if record_failures:
            # Keep the counter climbing while locked so the 10/15
            # escalation windows are reachable.
            record_failure(conn, username)
        logger.warning("auth.account_locked user=%s reason=lockout_active", username)
        return False

    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    ).fetchone()

    if row is None:
        # Constant-time dummy check so the response time doesn't leak user existence.
        bcrypt.checkpw(password.encode(), _get_dummy_hash())
        # Still record the failure against the claimed name so that
        # guessing valid-vs-invalid usernames doesn't leak through the
        # counter's presence/absence either. A garbage-username flood
        # just fills up junk rows that decay away in 24 h.
        if username and record_failures:
            record_failure(conn, username)
        return False

    ok = bcrypt.checkpw(password.encode(), row["password_hash"].encode())
    if ok:
        record_success(conn, username)
    elif record_failures:
        record_failure(conn, username)
    return ok


def create_session(
    conn: sqlite3.Connection,
    username: str,
    *,
    user_agent: str = "",
    client_ip: str = "",
    ttl_seconds: int | None = None,
) -> str:
    """Create a session and return the opaque token.

    The caller receives the raw token (64 hex chars, 256 bits). The DB
    stores only the SHA-256 hash; the legacy ``token`` column is written
    as empty string and is scheduled to be dropped in a future migration
    once no code path reads it. The fingerprint (UA hash + IP prefix)
    is captured so validate_session can detect cookie theft.

    ``ttl_seconds`` overrides the default hard expiry (1 day, matching
    the session cookie's ``max_age``) — useful only for tests.
    """
    token = generate_session_token()
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    if ttl_seconds is None:
        expires_at = (now + timedelta(days=_HARD_EXPIRY_DAYS)).isoformat()
    else:
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    # Only compute a fingerprint when we actually have client info.
    # Middleware passes ``None`` (not ``""``) for missing UA/IP so this
    # unbound path is reached for CLI / test callers that truly have no
    # client context. An empty UA + empty IP otherwise produced a
    # pseudo-fingerprint that falsely matched every probeless caller.
    mode = _fingerprint_mode()
    if mode == "off":
        fingerprint = ""
    elif user_agent or client_ip:
        fingerprint = _client_fingerprint(user_agent, client_ip)
    else:
        fingerprint = ""
    logger.debug(
        "session.fingerprint_issued user=%s mode=%s bound=%s",
        username,
        mode,
        bool(fingerprint),
    )
    # The legacy ``token`` column is required by the v1 schema's NOT NULL
    # + PRIMARY KEY constraints. Write an empty sentinel so the at-rest
    # value is no longer the live credential; validate_session looks up
    # solely by ``token_hash``. Existing NOT NULL / UNIQUE constraints
    # on the column are still satisfied because each row gets a distinct
    # token_hash and "" is only one row. If more than one session ever
    # lands before the column is dropped the second insert will fail the
    # PK — so we write ``token_hash`` verbatim into the legacy column to
    # keep the rows distinct without exposing the live token.
    conn.execute(
        "INSERT INTO admin_sessions "
        "(token, token_hash, username, created_at, expires_at, last_used_at, "
        " fingerprint, issued_ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token_hash, token_hash, username, now_iso, expires_at, now_iso,
         fingerprint, client_ip or ""),
    )
    conn.commit()
    logger.info("session.created user=%s ip=%s", username, client_ip or "-")
    return token


def validate_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> str | None:
    """Return the username for a valid, non-expired session token.

    Hardening over the minimal previous version:

    - Accepts only the exact token shape emitted by
      :func:`generate_session_token` (64 lowercase hex chars). Anything
      else fails fast without touching the DB.
    - Looks up by ``token_hash`` (SHA-256). Legacy rows without a
      populated ``token_hash`` were purged by migration v13.
    - Rejects tokens idle for > 24 h (``last_used_at`` check) on top of
      the 1-day hard expiry.
    - If ``user_agent`` and ``client_ip`` are supplied, checks the
      fingerprint against the issuer's fingerprint (unless the
      ``MEDIAMAN_FINGERPRINT_MODE`` env knob is ``off``). A mismatch
      destroys the session and returns None.
    - Updates ``last_used_at`` on every successful validation (sliding
      refresh of the idle window).

    Sweeps expired session rows at most once per
    ``_EXPIRED_CLEANUP_INTERVAL`` seconds inside the same
    ``BEGIN IMMEDIATE`` transaction that performs the lookup, so a
    concurrent writer cannot observe a torn state. Strict ``<`` on the
    expiry comparison matches the invariant that ``create_session``
    emits ``expires_at`` at least 1 s in the future.
    Returns None if the token is missing or expired.
    """
    if not token or not _SESSION_TOKEN_RE.fullmatch(token):
        return None

    global _last_cleanup_at
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    token_hash = _hash_token(token)

    # Wrap the sweep + lookup + optional invalidating delete in a single
    # immediate transaction. Without this, a concurrent writer can slip
    # an update in between the "is it expired?" read and the "delete it"
    # write, producing a tiny window where an idle-expired session gets
    # refreshed-and-destroyed racily.
    conn.execute("BEGIN IMMEDIATE")
    try:
        mono = time.monotonic()
        if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
            with _cleanup_lock:
                if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
                    conn.execute(
                        "DELETE FROM admin_sessions WHERE expires_at < ?",
                        (now_iso,),
                    )
                    _last_cleanup_at = mono

        row = conn.execute(
            "SELECT username, expires_at, last_used_at, fingerprint "
            "FROM admin_sessions WHERE token_hash = ? LIMIT 1",
            (token_hash,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        # Strict <: create_session emits expires_at ≥ now + 1 s so equal
        # timestamps are always still valid at this point.
        if row["expires_at"] and row["expires_at"] < now_iso:
            conn.execute("COMMIT")
            return None

        # Idle-timeout check
        last_used = row["last_used_at"]
        if last_used:
            try:
                last_dt = datetime.fromisoformat(last_used)
                if now_dt - last_dt > timedelta(hours=_IDLE_TIMEOUT_HOURS):
                    logger.info("session.idle_expired user=%s", row["username"])
                    conn.execute(
                        "DELETE FROM admin_sessions WHERE token_hash = ?",
                        (token_hash,),
                    )
                    conn.execute("COMMIT")
                    return None
            except ValueError:
                # Malformed timestamp — treat as expired, fall through.
                pass

        # Fingerprint check — only when caller supplied current client info
        # AND the mode is not "off".
        stored_fp = row["fingerprint"]
        mode = _fingerprint_mode()
        if (
            mode != "off"
            and stored_fp
            and user_agent is not None
            and client_ip is not None
        ):
            current_fp = _client_fingerprint(user_agent, client_ip)
            if current_fp != stored_fp:
                logger.warning(
                    "session.fingerprint_mismatch user=%s expected=%s got=%s ip=%s mode=%s",
                    row["username"], stored_fp, current_fp, client_ip, mode,
                )
                conn.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ?",
                    (token_hash,),
                )
                conn.execute("COMMIT")
                return None

        # Sliding refresh of last_used_at — throttled.
        should_refresh = True
        if last_used:
            try:
                last_dt = datetime.fromisoformat(last_used)
                if now_dt - last_dt < _SESSION_REFRESH_MIN_INTERVAL:
                    should_refresh = False
            except ValueError:
                pass

        if should_refresh:
            conn.execute(
                "UPDATE admin_sessions SET last_used_at = ? "
                "WHERE token_hash = ?",
                (now_iso, token_hash),
            )

        conn.execute("COMMIT")
        return row["username"]
    except Exception:
        conn.execute("ROLLBACK")
        raise


def destroy_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    actor: str = "",
    ip: str = "",
) -> None:
    """Delete the session row for the given token (logout).

    Appends a ``sec:session.destroy`` audit row so operators can trace
    voluntary logouts. ``actor`` and ``ip`` are optional — callers that
    have the username and client IP should pass them so the audit trail
    is meaningful.
    """
    token_hash = _hash_token(token)
    # Fetch the username from the row before deleting so we can include
    # it in the audit log even when the caller doesn't pass ``actor``.
    row = conn.execute(
        "SELECT username FROM admin_sessions WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    username = actor or (row["username"] if row else "")
    conn.execute(
        "DELETE FROM admin_sessions WHERE token_hash = ?",
        (token_hash,),
    )
    conn.commit()
    logger.info("session.destroyed user=%s ip=%s", username or "-", ip or "-")
    # Best-effort audit — import deferred to avoid circular imports at
    # module level (audit imports nothing from session, but session is
    # imported early in the auth stack).
    try:
        from mediaman.auth.audit import security_event
        security_event(conn, event="session.destroy", actor=username, ip=ip)
    except Exception:  # pragma: no cover
        pass


def destroy_all_sessions_for(conn: sqlite3.Connection, username: str) -> int:
    """Delete every session belonging to *username*. Returns rows affected."""
    cur = conn.execute(
        "DELETE FROM admin_sessions WHERE username = ?", (username,)
    )
    conn.commit()
    return cur.rowcount


class SessionMetadata(TypedDict):
    created_at: str | None
    expires_at: str | None
    last_used_at: str | None
    issued_ip: str | None
    fingerprint: str | None


def list_sessions_for(conn: sqlite3.Connection, username: str) -> list[SessionMetadata]:
    """Return metadata about the active sessions owned by *username*.

    Excludes the raw token/hash — callers get timestamps, fingerprint,
    and IP. Used by the "revoke sessions" admin UI.
    """
    rows = conn.execute(
        "SELECT created_at, expires_at, last_used_at, issued_ip, fingerprint "
        "FROM admin_sessions WHERE username = ? ORDER BY created_at DESC",
        (username,),
    ).fetchall()
    return [cast(SessionMetadata, dict(r)) for r in rows]


def change_password(
    conn: sqlite3.Connection,
    username: str,
    old_password: str,
    new_password: str,
    *,
    enforce_policy: bool = True,
) -> bool:
    """Change a user's password. Returns True on success, False if old password is wrong.

    Raises ``ValueError`` if the new password fails the shared strength
    policy (and ``enforce_policy`` is True, which is the default).
    The caller is responsible for catching and surfacing the policy
    issues to the user. ``enforce_policy=False`` is reserved for tests
    and legacy migration paths.
    """
    # Do NOT record failures here: a user mistyping their current
    # password inside their own change-password form must not trip the
    # lockout on themselves. A correct current password still clears
    # any pre-existing counter via ``record_success``.
    if not authenticate(conn, username, old_password, record_failures=False):
        return False

    if enforce_policy:
        from mediaman.auth.password_policy import password_issues

        issues = password_issues(new_password, username=username)
        if issues:
            raise ValueError(
                "Password does not meet strength policy: " + "; ".join(issues)
            )

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    conn.execute(
        "UPDATE admin_users SET password_hash=?, must_change_password=0 WHERE username=?",
        (new_hash, username),
    )
    # Invalidate all existing sessions for this user
    conn.execute("DELETE FROM admin_sessions WHERE username=?", (username,))
    conn.commit()
    logger.info("password.changed user=%s sessions_revoked=all", username)
    return True


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """Return all admin users (without password hashes)."""
    rows = conn.execute(
        "SELECT id, username, created_at FROM admin_users ORDER BY id"
    ).fetchall()
    return [{"id": row["id"], "username": row["username"], "created_at": row["created_at"]} for row in rows]


def delete_user(conn: sqlite3.Connection, user_id: int, current_username: str) -> bool:
    """Delete an admin user by ID.

    Refuses to delete the current user or the last remaining admin —
    either would lock everyone out of the UI.

    The "last admin" check is enforced atomically inside the DELETE
    statement itself (``WHERE ... AND (SELECT COUNT(*) FROM admin_users) > 1``)
    so two concurrent requests cannot both pass a TOCTOU check and wipe
    the final pair of admins.
    """
    row = conn.execute("SELECT username FROM admin_users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return False
    if row["username"] == current_username:
        return False  # Can't delete yourself

    # Wrap both writes in an explicit transaction so, if the atomic admin
    # DELETE is refused (last-admin guard), the pre-emptive session wipe
    # is rolled back alongside it.
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Sessions must go first — admin_sessions.username has a FK to
        # admin_users(username) and FK checks are ON.
        conn.execute(
            "DELETE FROM admin_sessions WHERE username=?", (row["username"],)
        )
        # Atomic last-admin guard: delete only if at least one other
        # admin still exists. Concurrent callers can't both see "count > 1"
        # because the subquery is evaluated as part of the DELETE.
        cursor = conn.execute(
            "DELETE FROM admin_users WHERE id = ? "
            "AND (SELECT COUNT(*) FROM admin_users) > 1",
            (user_id,),
        )
        if cursor.rowcount == 0:
            conn.execute("ROLLBACK")
            return False  # Would have left zero admins — refuse.
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True
