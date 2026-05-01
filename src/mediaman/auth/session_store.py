"""Admin session persistence, validation, and hardening.

Split from ``auth/session.py`` (R2). Owns the "how are sessions
persisted and validated" concern; password hashing lives in
:mod:`mediaman.auth.password_hash`.
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

from mediaman.crypto import generate_session_token

logger = logging.getLogger("mediaman")

_EXPIRED_CLEANUP_INTERVAL = 60.0
_last_cleanup_at = 0.0
_cleanup_lock = threading.Lock()

_SESSION_REFRESH_MIN_INTERVAL = timedelta(seconds=60)

_HARD_EXPIRY_DAYS = 1
_IDLE_TIMEOUT_HOURS = 24

_FINGERPRINT_MODE_ENV = "MEDIAMAN_FINGERPRINT_MODE"
_VALID_FINGERPRINT_MODES = {"strict", "loose", "off"}

_SESSION_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def _fingerprint_mode() -> str:
    """Return the current fingerprint mode from the environment."""
    mode = (os.environ.get(_FINGERPRINT_MODE_ENV) or "loose").lower()
    if mode not in _VALID_FINGERPRINT_MODES:
        return "loose"
    return mode


def _hash_token(token: str) -> str:
    """Return a SHA-256 hex digest of the token for at-rest storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def _client_fingerprint(user_agent: str | None, client_ip: str | None) -> str:
    """Compute a stable fingerprint for session-to-client binding."""
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


def create_session(
    conn: sqlite3.Connection,
    username: str,
    *,
    user_agent: str = "",
    client_ip: str = "",
    ttl_seconds: int | None = None,
) -> str:
    """Create a session and return the opaque token."""
    token = generate_session_token()
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    if ttl_seconds is None:
        expires_at = (now + timedelta(days=_HARD_EXPIRY_DAYS)).isoformat()
    else:
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
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
    conn.execute(
        "INSERT INTO admin_sessions "
        "(token, token_hash, username, created_at, expires_at, last_used_at, "
        " fingerprint, issued_ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            token_hash,
            token_hash,
            username,
            now_iso,
            expires_at,
            now_iso,
            fingerprint,
            client_ip or "",
        ),
    )
    conn.commit()
    logger.info("session.created user=%s ip=%s", username, client_ip or "-")
    return token


def _parse_last_used(raw: str | None, username: str) -> datetime | None:
    """Parse ``last_used_at`` from ISO format; return ``None`` on a corrupt value.

    Logs a warning and returns ``None`` if the stored timestamp cannot be
    parsed.  Callers that need fail-closed behaviour (idle-expiry check)
    must treat a ``None`` return as a signal to invalidate the session.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        logger.warning(
            "session.corrupt_last_used user=%s last_used_at=%r",
            username,
            raw,
        )
        return None


def _exec_with_commit(conn: sqlite3.Connection, sql: str, params: tuple) -> None:
    """Run *sql* inside a short ``BEGIN IMMEDIATE`` write transaction.

    Used by the validate-session fast path so a successful read does
    not need a writer slot, but the rare write paths (idle delete,
    last_used refresh, expired sweep, fingerprint mismatch) still
    serialise through SQLite cleanly. A failure inside the body is
    rolled back and re-raised; the caller decides whether to log or
    propagate.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(sql, params)
        conn.execute("COMMIT")
    except Exception:
        # ``rollback()`` (the high-level method) is a no-op when no
        # transaction is open, so we use it instead of a raw SQL
        # ROLLBACK + nested try/except — saves the bandit B110 noise
        # without losing the safety net.
        conn.rollback()
        raise


def _delete_session_with_commit(conn: sqlite3.Connection, token_hash: str) -> None:
    """Delete a session row inside its own short write transaction.

    Also revokes the matching reauth ticket so that a session killed by
    idle-expiry or fingerprint mismatch cannot leave an attacker who
    holds the stolen cookie + ticket pair with a working privileged
    window for the rest of the ticket's TTL (H-4).
    """
    _exec_with_commit(
        conn,
        "DELETE FROM admin_sessions WHERE token_hash = ?",
        (token_hash,),
    )
    try:
        from mediaman.auth.reauth import revoke_reauth_by_hash

        revoke_reauth_by_hash(conn, token_hash)
    except Exception:  # pragma: no cover — never let reauth cleanup block session destroy
        logger.debug("session.delete: revoke_reauth_by_hash failed", exc_info=True)


def _refresh_last_used_with_commit(conn: sqlite3.Connection, token_hash: str, now_iso: str) -> None:
    """Stamp last_used_at inside its own short write transaction."""
    _exec_with_commit(
        conn,
        "UPDATE admin_sessions SET last_used_at = ? WHERE token_hash = ?",
        (now_iso, token_hash),
    )


def _cleanup_expired_with_commit(conn: sqlite3.Connection, now_iso: str) -> None:
    """Sweep expired session rows inside a short write transaction.

    The matching reauth tickets are also swept so the table cannot grow
    indefinitely with rows whose owning session is gone (H-4).  The
    reauth sweep is best-effort — a failure here never aborts the
    session sweep.
    """
    _exec_with_commit(
        conn,
        "DELETE FROM admin_sessions WHERE expires_at < ?",
        (now_iso,),
    )
    try:
        from mediaman.auth.reauth import cleanup_expired_reauth

        cleanup_expired_reauth(conn, now_iso)
    except Exception:  # pragma: no cover
        logger.debug("session.cleanup: cleanup_expired_reauth failed", exc_info=True)


def validate_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> str | None:
    """Return the username for a valid, non-expired session token.

    Finding 26: ordinary requests must not take a SQLite writer lock.
    Validation is a read-only SELECT by default; write transactions are
    only opened when state actually changes (idle/expired delete,
    fingerprint mismatch delete, last_used_at refresh, periodic
    expired-row sweep).  This means a quiet stream of authenticated
    requests no longer blocks one another or competes with the
    scheduler for the WAL writer slot.
    """
    if not token or not _SESSION_TOKEN_RE.fullmatch(token):
        return None

    global _last_cleanup_at
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    token_hash = _hash_token(token)

    # Phase 1: read-only inspection. No BEGIN IMMEDIATE here — a vanilla
    # SELECT against a WAL-mode SQLite is concurrent with writers.
    row = conn.execute(
        "SELECT username, expires_at, last_used_at, fingerprint "
        "FROM admin_sessions WHERE token_hash = ? LIMIT 1",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] < now_iso:
        return None

    last_dt: datetime | None = _parse_last_used(row["last_used_at"], row["username"])

    # Phase 2: idle-expiry — short write transaction only when the
    # session actually has to be invalidated.
    if last_dt is None and row["last_used_at"]:
        # Corrupt timestamp — fail closed.
        logger.info("session.idle_expired user=%s reason=corrupt_timestamp", row["username"])
        _delete_session_with_commit(conn, token_hash)
        return None
    if last_dt is not None and now_dt - last_dt > timedelta(hours=_IDLE_TIMEOUT_HOURS):
        logger.info("session.idle_expired user=%s", row["username"])
        _delete_session_with_commit(conn, token_hash)
        return None

    # Phase 3: fingerprint check — read-only comparison; only the
    # mismatch branch reaches for the writer lock.
    stored_fp = row["fingerprint"]
    mode = _fingerprint_mode()
    if mode != "off" and stored_fp and user_agent is not None and client_ip is not None:
        current_fp = _client_fingerprint(user_agent, client_ip)
        if current_fp != stored_fp:
            logger.warning(
                "session.fingerprint_mismatch user=%s expected=%s got=%s ip=%s mode=%s",
                row["username"],
                stored_fp,
                current_fp,
                client_ip,
                mode,
            )
            _delete_session_with_commit(conn, token_hash)
            return None

    # Phase 4: last_used_at refresh — only writes when the throttle
    # interval has actually elapsed, so a rapid burst of requests by
    # the same session never queues up serial write transactions.
    needs_refresh = last_dt is None or now_dt - last_dt >= _SESSION_REFRESH_MIN_INTERVAL
    if needs_refresh:
        try:
            _refresh_last_used_with_commit(conn, token_hash, now_iso)
        except Exception:
            logger.warning(
                "session.last_used_at_refresh_failed user=%s",
                row["username"],
                exc_info=True,
            )

    # Phase 5: opportunistic expired-row sweep, gated on a monotonic
    # counter so it runs at most once per minute regardless of request
    # rate.
    mono = time.monotonic()
    if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
        with _cleanup_lock:
            if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
                try:
                    _cleanup_expired_with_commit(conn, now_iso)
                finally:
                    # Always advance the counter so a perpetual lock
                    # contention doesn't make us hammer the writer.
                    _last_cleanup_at = mono

    return row["username"]


def destroy_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    actor: str = "",
    ip: str = "",
) -> None:
    """Delete the session row for the given token (logout).

    Also revokes the matching reauth ticket — without that, a stolen
    session cookie plus a freshly granted reauth ticket would remain
    replayable for the rest of the ticket's TTL after the legitimate
    user has logged out (H-4).
    """
    token_hash = _hash_token(token)
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
    try:
        from mediaman.auth.reauth import revoke_reauth_by_hash

        revoke_reauth_by_hash(conn, token_hash)
    except Exception:  # pragma: no cover
        logger.debug("session.destroy: revoke_reauth_by_hash failed", exc_info=True)
    logger.info("session.destroyed user=%s ip=%s", username or "-", ip or "-")
    try:
        from mediaman.audit import security_event

        security_event(conn, event="session.destroy", actor=username, ip=ip)
    except Exception:  # pragma: no cover — audit is best-effort; never block logout
        logger.debug("session.destroy audit logging failed", exc_info=True)


def destroy_all_sessions_for(conn: sqlite3.Connection, username: str) -> int:
    """Delete every session belonging to *username*. Returns rows affected.

    Also revokes every reauth ticket owned by *username* (H-4) so a bulk
    session purge — typically driven by a forced password change or admin
    action — does not leave stale tickets that an attacker holding a
    related cookie could replay.
    """
    cur = conn.execute("DELETE FROM admin_sessions WHERE username = ?", (username,))
    conn.commit()
    try:
        from mediaman.auth.reauth import revoke_all_reauth_for

        revoke_all_reauth_for(conn, username)
    except Exception:  # pragma: no cover
        logger.debug(
            "session.destroy_all: revoke_all_reauth_for failed user=%s", username, exc_info=True
        )
    return cur.rowcount


class SessionMetadata(TypedDict):
    created_at: str | None
    expires_at: str | None
    last_used_at: str | None
    issued_ip: str | None
    fingerprint: str | None


def list_sessions_for(conn: sqlite3.Connection, username: str) -> list[SessionMetadata]:
    """Return metadata about the active sessions owned by *username*."""
    rows = conn.execute(
        "SELECT created_at, expires_at, last_used_at, issued_ip, fingerprint "
        "FROM admin_sessions WHERE username = ? ORDER BY created_at DESC",
        (username,),
    ).fetchall()
    return [cast(SessionMetadata, dict(r)) for r in rows]
