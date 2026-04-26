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


def _check_idle_expiry(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    last_dt: datetime | None,
    now_dt: datetime,
    token_hash: str,
    now_iso: str,
) -> bool:
    """Return ``True`` (and delete the session) if the session has idled out.

    A ``None`` ``last_dt`` (corrupt timestamp) is treated as expired — fail
    closed so a tampered timestamp cannot bypass the idle timeout.
    """
    if last_dt is None and row["last_used_at"]:
        # Corrupt timestamp — already logged by _parse_last_used; invalidate.
        logger.info("session.idle_expired user=%s reason=corrupt_timestamp", row["username"])
        conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))
        conn.execute("COMMIT")
        return True
    if last_dt is not None and now_dt - last_dt > timedelta(hours=_IDLE_TIMEOUT_HOURS):
        logger.info("session.idle_expired user=%s", row["username"])
        conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))
        conn.execute("COMMIT")
        return True
    return False


def _maybe_refresh_last_used(
    conn: sqlite3.Connection,
    last_dt: datetime | None,
    now_dt: datetime,
    token_hash: str,
    now_iso: str,
) -> None:
    """Write an updated ``last_used_at`` timestamp unless the session was just refreshed.

    Skips the write when the last refresh was within
    :data:`_SESSION_REFRESH_MIN_INTERVAL` to avoid hammering the DB on
    every request.  If ``last_dt`` is ``None`` (no prior timestamp or
    corrupt value), a fresh timestamp is always written.
    """
    if last_dt is not None and now_dt - last_dt < _SESSION_REFRESH_MIN_INTERVAL:
        return
    conn.execute(
        "UPDATE admin_sessions SET last_used_at = ? WHERE token_hash = ?",
        (now_iso, token_hash),
    )


def validate_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> str | None:
    """Return the username for a valid, non-expired session token."""
    if not token or not _SESSION_TOKEN_RE.fullmatch(token):
        return None

    global _last_cleanup_at
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    token_hash = _hash_token(token)

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
        if row["expires_at"] and row["expires_at"] < now_iso:
            conn.execute("COMMIT")
            return None

        # Parse last_used_at once; reused for idle-expiry and refresh-interval checks.
        last_dt: datetime | None = _parse_last_used(row["last_used_at"], row["username"])

        if _check_idle_expiry(conn, row, last_dt, now_dt, token_hash, now_iso):
            return None

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
                conn.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ?",
                    (token_hash,),
                )
                conn.execute("COMMIT")
                return None

        _maybe_refresh_last_used(conn, last_dt, now_dt, token_hash, now_iso)

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
    """Delete the session row for the given token (logout)."""
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
    logger.info("session.destroyed user=%s ip=%s", username or "-", ip or "-")
    try:
        from mediaman.audit import security_event

        security_event(conn, event="session.destroy", actor=username, ip=ip)
    except Exception:  # pragma: no cover — audit is best-effort; never block logout
        logger.debug("session.destroy audit logging failed", exc_info=True)


def destroy_all_sessions_for(conn: sqlite3.Connection, username: str) -> int:
    """Delete every session belonging to *username*. Returns rows affected."""
    cur = conn.execute("DELETE FROM admin_sessions WHERE username = ?", (username,))
    conn.commit()
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
