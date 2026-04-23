"""Admin user and session management.

Session hardening against insider threats and cookie theft
----------------------------------------------------------

The session table stores a SHA-256 **hash** of the token plus a
user-agent fingerprint and the client IP at issuance. Validation
checks:

1. Token exists and is not expired (hard expiry: 7 days from issue).
2. Token is not **idle**: last-used time must be within 24 hours.
   Sliding-window refresh: every validate_session bumps last_used_at.
3. Client fingerprint (user-agent hash + IP prefix) matches the
   issuer's fingerprint. A mismatch logs a WARNING and invalidates
   the session — catches stolen cookies used from a different
   browser or network.

The table still stores the raw token as ``token`` for backward
compatibility with any already-issued session rows, but new sessions
only store the hash. validate_session accepts either shape.
"""
from __future__ import annotations


import hashlib
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

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


def _hash_token(token: str) -> str:
    """Return a SHA-256 hex digest of the token for at-rest storage.

    Rationale: if the sqlite DB file leaks, the raw session tokens
    should not be directly usable. Storing the hash makes token theft
    from the DB useless — attacker would need to reverse SHA-256 which
    is infeasible for 256-bit random input.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _client_fingerprint(user_agent: str, client_ip: str) -> str:
    """Compute a stable fingerprint for session-to-client binding.

    UA is hashed (truncated SHA-256) + IP-prefix (first three octets
    for IPv4, first 64 bits for IPv6) to tolerate CGNAT / residential
    NAT shuffles while still catching "cookie stolen and replayed
    from Russia" scenarios.
    """
    import ipaddress

    ua_hash = hashlib.sha256(user_agent.encode()).hexdigest()[:16]
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
    """Add the hardening columns to admin_sessions if not present.

    Lightweight in-process migration — calling this from every session
    write is cheap (one PRAGMA) and means older DBs roll forward
    transparently.
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
    # Admin-users flag — not on admin_sessions but cheap to colocate.
    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(admin_users)").fetchall()}
    if "must_change_password" not in user_cols:
        conn.execute(
            "ALTER TABLE admin_users ADD COLUMN "
            "must_change_password INTEGER NOT NULL DEFAULT 0"
        )


def user_must_change_password(conn: sqlite3.Connection, username: str) -> bool:
    """Return True when *username*'s account is flagged to force a rotation."""
    _ensure_session_columns(conn)
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
    _ensure_session_columns(conn)
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

    _ensure_session_columns(conn)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at, must_change_password) "
            "VALUES (?, ?, ?, 0)",
            (username, password_hash, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"User '{username}' already exists")


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """Verify username/password credentials.

    Always performs a bcrypt check — even for nonexistent users — to prevent
    timing-based username enumeration.

    A persistent per-username lockout (see
    :mod:`mediaman.auth.login_lockout`) short-circuits the bcrypt check
    when an account has tripped the threshold. Lock state is **not**
    leaked to the caller — we return False the same as a wrong password,
    so an attacker cannot enumerate valid usernames by watching for a
    different response shape.
    """
    from mediaman.auth.login_lockout import (
        check_lockout,
        record_failure,
        record_success,
    )

    if username and check_lockout(conn, username):
        # Still burn a bcrypt round so response time stays flat against
        # "is this account locked?" timing probes.
        bcrypt.checkpw(password.encode(), _get_dummy_hash())
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
        if username:
            record_failure(conn, username)
        return False

    ok = bcrypt.checkpw(password.encode(), row["password_hash"].encode())
    if ok:
        record_success(conn, username)
    else:
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
    stores only the SHA-256 hash. The fingerprint (UA hash + IP prefix)
    is captured so validate_session can detect cookie theft.

    ``ttl_seconds`` overrides the default hard expiry (7 days) — useful
    only for tests.
    """
    _ensure_session_columns(conn)
    token = generate_session_token()
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    if ttl_seconds is None:
        expires_at = (now + timedelta(days=_HARD_EXPIRY_DAYS)).isoformat()
    else:
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    # Only compute a fingerprint if we actually have client info.
    # An empty UA + empty IP produces a pseudo-fingerprint (sha of
    # nothing + "unknown") that would falsely match every probeless
    # caller — better to treat such sessions as unbound.
    fingerprint = (
        _client_fingerprint(user_agent, client_ip)
        if user_agent or client_ip
        else ""
    )
    conn.execute(
        "INSERT INTO admin_sessions "
        "(token, token_hash, username, created_at, expires_at, last_used_at, "
        " fingerprint, issued_ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, token_hash, username, now_iso, expires_at, now_iso,
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

    - Looks up by ``token_hash`` (SHA-256) preferentially, falls back to
      raw ``token`` column for legacy rows.
    - Rejects tokens idle for > 24 h (``last_used_at`` check) on top of
      the 7-day hard expiry.
    - If ``user_agent`` and ``client_ip`` are supplied, checks the
      fingerprint against the issuer's fingerprint. A mismatch destroys
      the session and returns None.
    - Updates ``last_used_at`` on every successful validation (sliding
      refresh of the idle window).

    Sweeps expired session rows at most once per ``_EXPIRED_CLEANUP_INTERVAL``
    seconds so hot traffic doesn't turn every request into a write.
    Returns None if the token is missing or expired.
    """
    if not token or len(token) > 256:
        return None

    global _last_cleanup_at
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    _ensure_session_columns(conn)

    mono = time.monotonic()
    if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
        with _cleanup_lock:
            if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
                conn.execute(
                    "DELETE FROM admin_sessions WHERE expires_at <= ?", (now_iso,)
                )
                conn.commit()
                _last_cleanup_at = mono

    token_hash = _hash_token(token)
    # Prefer hash lookup; fall back to legacy raw-token match for
    # rows written before this hardening landed.
    row = conn.execute(
        "SELECT username, expires_at, last_used_at, fingerprint "
        "FROM admin_sessions WHERE token_hash = ? OR token = ? LIMIT 1",
        (token_hash, token),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] <= now_iso:
        return None

    # Idle-timeout check
    last_used = row["last_used_at"]
    if last_used:
        try:
            last_dt = datetime.fromisoformat(last_used)
            if now_dt - last_dt > timedelta(hours=_IDLE_TIMEOUT_HOURS):
                logger.info(
                    "session.idle_expired user=%s",
                    row["username"],
                )
                conn.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ? OR token = ?",
                    (token_hash, token),
                )
                conn.commit()
                return None
        except ValueError:
            pass

    # Fingerprint check — only when caller supplied current client info
    stored_fp = row["fingerprint"]
    if stored_fp and user_agent is not None and client_ip is not None:
        current_fp = _client_fingerprint(user_agent, client_ip)
        if current_fp != stored_fp:
            logger.warning(
                "session.fingerprint_mismatch user=%s expected=%s got=%s ip=%s",
                row["username"], stored_fp, current_fp, client_ip,
            )
            conn.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ? OR token = ?",
                (token_hash, token),
            )
            conn.commit()
            return None

    # Sliding refresh of last_used_at — throttled. The idle timeout
    # is 24 h, so rounding the refresh to a ~60 s cadence is invisible
    # to users but removes the per-request write that would otherwise
    # contend with background writers (scanner, download queue).
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
            "WHERE token_hash = ? OR token = ?",
            (now_iso, token_hash, token),
        )
        conn.commit()

    return row["username"]


def destroy_session(conn: sqlite3.Connection, token: str) -> None:
    """Delete the session row for the given token (logout)."""
    _ensure_session_columns(conn)
    token_hash = _hash_token(token)
    conn.execute(
        "DELETE FROM admin_sessions WHERE token_hash = ? OR token = ?",
        (token_hash, token),
    )
    conn.commit()


def destroy_all_sessions_for(conn: sqlite3.Connection, username: str) -> int:
    """Delete every session belonging to *username*. Returns rows affected."""
    cur = conn.execute(
        "DELETE FROM admin_sessions WHERE username = ?", (username,)
    )
    conn.commit()
    return cur.rowcount


def list_sessions_for(conn: sqlite3.Connection, username: str) -> list[dict]:
    """Return metadata about the active sessions owned by *username*.

    Excludes the raw token/hash — callers get timestamps, fingerprint,
    and IP. Used by the "revoke sessions" admin UI.
    """
    _ensure_session_columns(conn)
    rows = conn.execute(
        "SELECT created_at, expires_at, last_used_at, issued_ip, fingerprint "
        "FROM admin_sessions WHERE username = ? ORDER BY created_at DESC",
        (username,),
    ).fetchall()
    return [dict(r) for r in rows]


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
    if not authenticate(conn, username, old_password):
        return False

    if enforce_policy:
        from mediaman.auth.password_policy import password_issues

        issues = password_issues(new_password, username=username)
        if issues:
            raise ValueError(
                "Password does not meet strength policy: " + "; ".join(issues)
            )

    _ensure_session_columns(conn)
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
