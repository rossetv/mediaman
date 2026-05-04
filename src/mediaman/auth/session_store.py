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
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from mediaman.auth._token_hashing import hash_token as _hash_token
from mediaman.crypto import generate_session_token
from mediaman.services.infra.format import parse_iso_utc as _parse_iso_aware

# ``_parse_iso_aware`` is now an alias for the canonical
# :func:`mediaman.services.infra.format.parse_iso_utc`. The forensic
# ``_parse_last_used`` below stays bespoke because it must log a warning
# when a stored timestamp is corrupt — a side effect the generic parser
# deliberately does not perform.

logger = logging.getLogger("mediaman")

_EXPIRED_CLEANUP_INTERVAL = 60.0
_last_cleanup_at = 0.0
_cleanup_lock = threading.Lock()

_SESSION_REFRESH_MIN_INTERVAL = timedelta(seconds=60)

_HARD_EXPIRY_DAYS = 1
_IDLE_TIMEOUT_HOURS = 24

_FINGERPRINT_MODE_ENV = "MEDIAMAN_FINGERPRINT_MODE"
#: Supported fingerprint modes.  Each one trades resilience against
#: legitimate client churn for binding strength:
#:
#: ``off``    — no binding at all.  ``fingerprint`` is stored empty and
#:              the validate-side comparison is skipped.  Useful for
#:              deployments behind reverse-proxy farms that rewrite
#:              client IPs unpredictably or where every legitimate
#:              client is on a churn-heavy CGNAT.
#:
#: ``loose``  — IPv4 bucketed at ``/24`` and IPv6 at ``/64``; UA hash
#:              truncated to 16 hex chars.  This is the default.  It
#:              tolerates an end user roaming inside a single carrier
#:              CGNAT pool and minor UA churn (Chrome version bumps mid
#:              session) without invalidating the cookie, while still
#:              shutting down a stolen-cookie replay from a different
#:              network or a different browser family.
#:
#: ``strict`` — full client IP (no bucketing) and full SHA-256 UA hash
#:              (no truncation).  Maximum binding strength but
#:              intolerant of CGNAT IP rotation and any UA churn at
#:              all (User-Agent string changes, Chrome version bumps,
#:              switching from desktop to mobile UA on the same
#:              network).  Choose this when every legitimate client
#:              has a stable public IP and a stable UA.
_VALID_FINGERPRINT_MODES = {"strict", "loose", "off"}

#: Per-mode bucket configuration consumed by :func:`_client_fingerprint`.
#: ``ipv4_prefix`` / ``ipv6_prefix`` — CIDR length to bucket the client
#: IP at; ``None`` means "use the full address with no bucketing".
#: ``ua_hash_chars`` — number of leading hex chars of the SHA-256 UA
#: hash to keep; ``None`` means "use the full 64-char digest".
_FINGERPRINT_BUCKETS: dict[str, dict[str, int | None]] = {
    "loose": {"ipv4_prefix": 24, "ipv6_prefix": 64, "ua_hash_chars": 16},
    "strict": {"ipv4_prefix": None, "ipv6_prefix": None, "ua_hash_chars": None},
}

# Anchors are redundant under ``fullmatch``; using a bare token regex
# here means the cheap pre-DB sanity check on every authenticated
# request stays cheap.
_SESSION_TOKEN_RE = re.compile(r"[0-9a-f]{64}")


def _fingerprint_mode() -> str:
    """Return the current fingerprint mode from the environment."""
    mode = (os.environ.get(_FINGERPRINT_MODE_ENV) or "loose").lower()
    if mode not in _VALID_FINGERPRINT_MODES:
        return "loose"
    return mode


def _client_fingerprint(
    user_agent: str | None,
    client_ip: str | None,
    *,
    mode: str | None = None,
) -> str:
    """Compute a stable fingerprint for session-to-client binding.

    Dispatches on *mode* — defaults to the value of
    :func:`_fingerprint_mode` when the caller does not pin it.  ``off``
    is intentionally not handled here; create-/validate-side code
    branches on ``mode == 'off'`` before calling this helper.

    See :data:`_VALID_FINGERPRINT_MODES` for the documented trade-offs
    of each mode.
    """
    if mode is None:
        mode = _fingerprint_mode()
    bucket_cfg = _FINGERPRINT_BUCKETS.get(mode, _FINGERPRINT_BUCKETS["loose"])
    ua_hash_chars = bucket_cfg["ua_hash_chars"]
    full_ua_hash = hashlib.sha256((user_agent or "").encode()).hexdigest()
    ua_hash = full_ua_hash if ua_hash_chars is None else full_ua_hash[:ua_hash_chars]

    if not client_ip:
        prefix = "unknown"
    else:
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            prefix = "unknown"
        else:
            if isinstance(addr, ipaddress.IPv6Address):
                ipv6_prefix = bucket_cfg["ipv6_prefix"]
                if ipv6_prefix is None:
                    prefix = str(addr)
                else:
                    prefix = str(
                        ipaddress.ip_network(
                            f"{client_ip}/{ipv6_prefix}", strict=False
                        ).network_address
                    )
            else:
                ipv4_prefix = bucket_cfg["ipv4_prefix"]
                if ipv4_prefix is None:
                    prefix = str(addr)
                else:
                    prefix = str(
                        ipaddress.ip_network(
                            f"{client_ip}/{ipv4_prefix}", strict=False
                        ).network_address
                    )
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
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    if ttl_seconds is None:
        expires_at = (now + timedelta(days=_HARD_EXPIRY_DAYS)).isoformat()
    else:
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    mode = _fingerprint_mode()
    if mode == "off":
        fingerprint = ""
    elif user_agent or client_ip:
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
    """Delete a session row AND its reauth ticket atomically.

    Both the session row and the matching reauth ticket are deleted
    inside the SAME ``BEGIN IMMEDIATE`` transaction.  Splitting them
    across two transactions used to leave a window where the session
    was gone but the ticket survived — a stolen cookie + ticket pair
    would remain replayable for the rest of the ticket's TTL even
    though the legitimate session had been killed by idle expiry or
    fingerprint mismatch (H-4 + audit).

    The reauth-side delete is best-effort: if the
    ``revoke_reauth_by_hash`` helper itself raises (e.g. table
    missing), the whole transaction is rolled back and the caller
    sees the error.  That is safer than the previous ``except: log``
    swallow which could leave the ticket alive.
    """
    # Local import to dodge the session_store -> reauth import cycle.
    from mediaman.auth.reauth import revoke_reauth_by_hash_in_tx

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM admin_sessions WHERE token_hash = ?",
            (token_hash,),
        )
        revoke_reauth_by_hash_in_tx(conn, token_hash)
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise


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
    except Exception:
        logger.warning(
            "session.delete_failed reason=%s",
            reason,
            exc_info=True,
        )


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
    now_dt = datetime.now(UTC)
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
    expires_dt = _parse_iso_aware(row["expires_at"])
    if expires_dt is not None and expires_dt < now_dt:
        return None

    last_dt: datetime | None = _parse_last_used(row["last_used_at"], row["username"])

    # Phase 2: idle-expiry — short write transaction only when the
    # session actually has to be invalidated.
    if last_dt is None and row["last_used_at"]:
        # Corrupt timestamp — fail closed.
        logger.info("session.idle_expired user=%s reason=corrupt_timestamp", row["username"])
        _try_delete_session(conn, token_hash, reason="corrupt_timestamp")
        return None
    if last_dt is not None and now_dt - last_dt > timedelta(hours=_IDLE_TIMEOUT_HOURS):
        logger.info("session.idle_expired user=%s", row["username"])
        _try_delete_session(conn, token_hash, reason="idle_expired")
        return None

    # Phase 3: fingerprint check — read-only comparison; only the
    # mismatch branch reaches for the writer lock.
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
                    # Stamp the counter with the moment the cleanup
                    # FINISHED, not the moment validate_session was
                    # entered.  Otherwise a slow sweep would let the
                    # next request fire another sweep almost
                    # immediately after this one returned, defeating
                    # the once-per-minute throttle.
                    _last_cleanup_at = time.monotonic()

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

    The username lookup, session delete, and reauth-ticket delete are
    wrapped in a single ``BEGIN IMMEDIATE`` transaction so a failure
    on the reauth side rolls the session delete back too — there is
    no longer a window where the session is gone but the ticket
    survives.
    """
    # Local import to avoid the session_store -> reauth -> session
    # cycle at module load.
    from mediaman.auth.reauth import revoke_reauth_by_hash_in_tx

    token_hash = _hash_token(token)

    conn.execute("BEGIN IMMEDIATE")
    try:
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
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise

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
