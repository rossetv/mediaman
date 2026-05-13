"""Admin session persistence, validation, and hardening.

Split from ``auth/session.py`` (R2). Owns the "how are sessions
persisted and validated" concern; password hashing lives in
:mod:`mediaman.web.auth.password_hash`.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import TypedDict, cast

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.core.time import parse_iso_utc as _parse_iso_aware
from mediaman.crypto import generate_session_token
from mediaman.web.auth._session_fingerprint import (
    _client_fingerprint,
    _fingerprint_mode,
)
from mediaman.web.auth._token_hashing import hash_token as _hash_token

# ``_parse_iso_aware`` is now an alias for the canonical
# :func:`mediaman.core.format.parse_iso_utc`. The forensic
# ``_parse_last_used`` below stays bespoke because it must log a warning
# when a stored timestamp is corrupt — a side effect the generic parser
# deliberately does not perform.

# Re-export so that ``session.py`` and tests can import these from the
# canonical ``session_store`` module path, and monkeypatches on
# ``session_store._fingerprint_mode`` / ``session_store._client_fingerprint``
# continue to intercept calls made inside this module.
__all__ = [
    "_SESSION_TOKEN_RE",
    "_client_fingerprint",
    "_fingerprint_mode",
]

logger = logging.getLogger(__name__)

# Rate-limit state: tracks when the last expired-session sweep ran so consecutive
# requests on the same process only trigger a write transaction at most once per
# minute — kept global because the sweep is process-wide, not request-scoped.
_EXPIRED_CLEANUP_INTERVAL = 60.0
_last_cleanup_at = 0.0
_cleanup_lock = threading.Lock()

_SESSION_REFRESH_MIN_INTERVAL = timedelta(seconds=60)

_HARD_EXPIRY_DAYS = 1
_IDLE_TIMEOUT_HOURS = 24

# Anchors are redundant under ``fullmatch``; using a bare token regex
# here means the cheap pre-DB sanity check on every authenticated
# request stays cheap.
_SESSION_TOKEN_RE = re.compile(r"[0-9a-f]{64}")


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
    now = now_utc()
    now_iso = now.isoformat()
    if ttl_seconds is None:
        expires_at = (now + timedelta(days=_HARD_EXPIRY_DAYS)).isoformat()
    else:
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    mode = _fingerprint_mode()
    if mode != "off" and (user_agent or client_ip):
        fingerprint = _client_fingerprint(user_agent, client_ip, mode=mode)
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
    dt = parse_iso_strict_utc(raw)
    if dt is None:
        logger.warning(
            "session.corrupt_last_used user=%s last_used_at=%r",
            username,
            raw,
        )
    return dt


def _exec_with_commit(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> None:
    """Run *sql* inside a short ``BEGIN IMMEDIATE`` write transaction.

    Used by the validate-session fast path so a successful read does
    not need a writer slot, but the rare write paths (idle delete,
    last_used refresh, expired sweep, fingerprint mismatch) still
    serialise through SQLite cleanly. A failure inside the body is
    rolled back and re-raised; the caller decides whether to log or
    propagate.

    Contract:

    * The caller MUST NOT have an open transaction on *conn* when
      invoking this helper — sqlite3 will raise ``OperationalError:
      cannot start a transaction within a transaction`` if a
      ``BEGIN`` is already open.  All in-tree callers go through the
      validate-session entry point or one of the public mutation
      helpers, neither of which holds an outer transaction.
    * On success the inner work is committed before this function
      returns.
    * On any exception the transaction is rolled back via the
      high-level ``rollback()`` (a safe no-op when nothing is open) and
      the exception is re-raised.
    """
    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql, params)


def _delete_session_with_commit(conn: sqlite3.Connection, token_hash: str) -> None:
    """Delete a session row AND its reauth ticket atomically.

    Both the session row and the matching reauth ticket are deleted
    inside the SAME ``BEGIN IMMEDIATE`` transaction.  Splitting them
    across two transactions used to leave a window where the session
    was gone but the ticket survived — a stolen cookie + ticket pair
    would remain replayable for the rest of the ticket's TTL even
    though the legitimate session had been killed by idle expiry or
    fingerprint mismatch.

    The reauth-side delete is best-effort: if the
    ``revoke_reauth_by_hash`` helper itself raises (e.g. table
    missing), the whole transaction is rolled back and the caller
    sees the error.  That is safer than the previous ``except: log``
    swallow which could leave the ticket alive.
    """
    # Local import to dodge the session_store -> reauth import cycle.
    from mediaman.web.auth.reauth import revoke_reauth_by_hash_in_tx

    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM admin_sessions WHERE token_hash = ?",
            (token_hash,),
        )
        revoke_reauth_by_hash_in_tx(conn, token_hash)


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
    indefinitely with rows whose owning session is gone. The reauth sweep
    is best-effort — a failure here never aborts the session sweep.
    """
    _exec_with_commit(
        conn,
        "DELETE FROM admin_sessions WHERE expires_at < ?",
        (now_iso,),
    )
    # rationale: best-effort reauth sweep — a failure here must not abort the
    # session sweep that already ran; log and let the next sweep retry.
    try:
        from mediaman.web.auth.reauth import cleanup_expired_reauth

        cleanup_expired_reauth(conn, now_iso)
    except Exception:  # pragma: no cover
        logger.debug("session.cleanup: cleanup_expired_reauth failed", exc_info=True)


def _try_delete_session(conn: sqlite3.Connection, token_hash: str, *, reason: str) -> None:
    """Best-effort wrapper around :func:`_delete_session_with_commit`.

    The atomic delete-session-and-revoke-ticket operation can in
    principle raise (e.g. transient lock contention).  ``validate_session``
    must NOT bubble that exception up to the request handler — the user
    is already on the unhappy path and a 500 helps nobody.  Log the
    failure and let the next request retry.
    """
    try:
        _delete_session_with_commit(conn, token_hash)
    # rationale: best-effort session writes — bubbling this up would 500
    # the user during validation; log and let the next request retry.
    except Exception:
        logger.warning(
            "session.delete_failed reason=%s",
            reason,
            exc_info=True,
        )


def _fetch_session_row(conn: sqlite3.Connection, token_hash: str) -> sqlite3.Row | None:
    """Phase 1: read-only SELECT of the session row.

    No BEGIN IMMEDIATE here — a vanilla SELECT against a WAL-mode SQLite
    is concurrent with writers.
    """
    return cast(
        sqlite3.Row | None,
        conn.execute(
            "SELECT username, expires_at, last_used_at, fingerprint "
            "FROM admin_sessions WHERE token_hash = ? LIMIT 1",
            (token_hash,),
        ).fetchone(),
    )


def _idle_expired(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    last_dt: datetime | None,
    token_hash: str,
    now_dt: datetime,
) -> bool:
    """Phase 2: idle-expiry check; delete the session and return True when expired.

    Returns ``True`` if the caller must treat the session as invalid
    (corrupt timestamp or beyond the idle window) and a delete has been
    issued. Returns ``False`` when the session is still within its idle
    window.
    """
    if last_dt is None and row["last_used_at"]:
        # Corrupt timestamp — fail closed.
        logger.info("session.idle_expired user=%s reason=corrupt_timestamp", row["username"])
        _try_delete_session(conn, token_hash, reason="corrupt_timestamp")
        return True
    if last_dt is not None and now_dt - last_dt > timedelta(hours=_IDLE_TIMEOUT_HOURS):
        logger.info("session.idle_expired user=%s", row["username"])
        _try_delete_session(conn, token_hash, reason="idle_expired")
        return True
    return False


def _fingerprint_mismatch(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    token_hash: str,
    user_agent: str | None,
    client_ip: str | None,
) -> bool:
    """Phase 3: fingerprint check; delete the session and return True on mismatch.

    A read-only comparison by default; only the mismatch branch reaches
    for the writer lock to evict the bound session.
    """
    stored_fp = row["fingerprint"]
    mode = _fingerprint_mode()
    if mode != "off" and stored_fp and user_agent is not None and client_ip is not None:
        current_fp = _client_fingerprint(user_agent, client_ip, mode=mode)
        if current_fp != stored_fp:
            logger.warning(
                "session.fingerprint_mismatch user=%s expected=%s got=%s ip=%s mode=%s",
                row["username"],
                stored_fp,
                current_fp,
                client_ip,
                mode,
            )
            _try_delete_session(conn, token_hash, reason="fingerprint_mismatch")
            return True
    return False


def _maybe_refresh_last_used(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    last_dt: datetime | None,
    token_hash: str,
    now_dt: datetime,
    now_iso: str,
) -> None:
    """Phase 4: refresh ``last_used_at`` only when the throttle interval has elapsed.

    Throttling keeps a rapid burst of requests from queueing up serial
    write transactions on the same session.
    """
    needs_refresh = last_dt is None or now_dt - last_dt >= _SESSION_REFRESH_MIN_INTERVAL
    if not needs_refresh:
        return
    try:
        _refresh_last_used_with_commit(conn, token_hash, now_iso)
    # rationale: best-effort session writes — refreshing last_used_at
    # is a freshness signal, not a correctness gate; never fail the
    # request because the timestamp didn't update.
    except Exception:
        logger.warning(
            "session.last_used_at_refresh_failed user=%s",
            row["username"],
            exc_info=True,
        )


def _maybe_sweep_expired(conn: sqlite3.Connection, now_iso: str) -> None:
    """Phase 5: opportunistic expired-row sweep gated to ≤ once per minute.

    Invariant: stamp ``_last_cleanup_at`` with the moment the cleanup
    FINISHED, not the moment :func:`validate_session` was entered.
    Otherwise a slow sweep would let the next request fire another sweep
    almost immediately after this one returned, defeating the
    once-per-minute throttle.
    """
    global _last_cleanup_at
    mono = time.monotonic()
    if mono - _last_cleanup_at < _EXPIRED_CLEANUP_INTERVAL:
        return
    with _cleanup_lock:
        if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
            try:
                _cleanup_expired_with_commit(conn, now_iso)
            finally:
                _last_cleanup_at = time.monotonic()


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

    now_dt = now_utc()
    now_iso = now_dt.isoformat()
    token_hash = _hash_token(token)

    row = _fetch_session_row(conn, token_hash)
    if row is None:
        return None
    expires_dt = _parse_iso_aware(row["expires_at"])
    if expires_dt is not None and expires_dt < now_dt:
        return None

    last_dt: datetime | None = _parse_last_used(row["last_used_at"], row["username"])

    if _idle_expired(conn, row, last_dt, token_hash, now_dt):
        return None
    if _fingerprint_mismatch(conn, row, token_hash, user_agent, client_ip):
        return None

    _maybe_refresh_last_used(conn, row, last_dt, token_hash, now_dt, now_iso)
    _maybe_sweep_expired(conn, now_iso)

    return cast(str, row["username"])


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
    user has logged out.

    The username lookup, session delete, and reauth-ticket delete are
    wrapped in a single ``BEGIN IMMEDIATE`` transaction so a failure
    on the reauth side rolls the session delete back too — there is
    no longer a window where the session is gone but the ticket
    survives.
    """
    # Local import to avoid the session_store -> reauth -> session
    # cycle at module load.
    from mediaman.web.auth.reauth import revoke_reauth_by_hash_in_tx

    token_hash = _hash_token(token)

    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT username FROM admin_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        username = actor or (row["username"] if row else "")
        conn.execute(
            "DELETE FROM admin_sessions WHERE token_hash = ?",
            (token_hash,),
        )
        revoke_reauth_by_hash_in_tx(conn, token_hash)

    logger.info("session.destroyed user=%s ip=%s", username or "-", ip or "-")
    try:
        from mediaman.core.audit import security_event

        security_event(conn, event="session.destroy", actor=username, ip=ip)
    except Exception:  # pragma: no cover — audit is best-effort; never block logout
        logger.debug("session.destroy audit logging failed", exc_info=True)


def destroy_all_sessions_for(conn: sqlite3.Connection, username: str) -> int:
    """Delete every session belonging to *username*. Returns rows affected.

    Also revokes every reauth ticket owned by *username* so a bulk session
    purge — typically driven by a forced password change or admin action —
    does not leave stale tickets that an attacker holding a related cookie
    could replay.
    """
    cur = conn.execute("DELETE FROM admin_sessions WHERE username = ?", (username,))
    conn.commit()
    # rationale: best-effort reauth revocation — a failure here must not roll
    # back the bulk session delete that already committed; log and continue.
    try:
        from mediaman.web.auth.reauth import revoke_all_reauth_for

        revoke_all_reauth_for(conn, username)
    except Exception:  # pragma: no cover
        logger.debug(
            "session.destroy_all: revoke_all_reauth_for failed user=%s", username, exc_info=True
        )
    return cur.rowcount


class SessionMetadata(TypedDict):
    """Typed projection of an ``admin_sessions`` row for API/UI consumption.

    All fields are optional strings because SQLite may return ``NULL`` for
    rows inserted by older schema versions.  Consumers must guard against
    ``None`` before formatting timestamps or displaying the ``issued_ip``.

    Fields
    ------
    created_at:
        ISO-8601 UTC timestamp at which the session was first created.
    expires_at:
        ISO-8601 UTC hard-expiry timestamp.  The session becomes invalid
        once this instant passes regardless of recent activity.
    last_used_at:
        ISO-8601 UTC timestamp of the most recent successful
        ``validate_session`` call.  Drives idle-expiry eviction.
    issued_ip:
        Client IP address recorded at session creation, stored for audit
        purposes only — not re-validated on subsequent requests.
    fingerprint:
        Opaque string computed by :func:`_client_fingerprint` at session
        creation.  Empty when fingerprint mode is ``"off"`` or no
        client context was available at creation time.
    """

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
    # Build each ``SessionMetadata`` explicitly so a future column-type
    # drift surfaces as a type-checker error instead of being silently
    # papered over by ``cast()``.  The audited finding noted that
    # ``cast(SessionMetadata, dict(r))`` was a lie to mypy — the row
    # could carry any types and the cast would still pass.
    return [
        SessionMetadata(
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            last_used_at=r["last_used_at"],
            issued_ip=r["issued_ip"],
            fingerprint=r["fingerprint"],
        )
        for r in rows
    ]
