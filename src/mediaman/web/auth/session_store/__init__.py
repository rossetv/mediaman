"""Admin session persistence, validation, and hardening — re-export barrel.

Split from ``auth/session.py`` (R2). Owns the "how are sessions
persisted and validated" concern; password hashing lives in
:mod:`mediaman.web.auth.password_hash`.

This package was promoted from a single ``session_store.py`` module when
it crossed the 500-line ceiling. The public surface — ``create_session``,
``validate_session``, ``destroy_session``, ``destroy_all_sessions_for``,
``list_sessions_for`` and the :class:`SessionMetadata` projection — plus
the constants tests read directly (``_SESSION_TOKEN_RE``,
``_HARD_EXPIRY_DAYS``) stay in this barrel. The private helpers moved to
two responsibility modules:

* :mod:`._writes` — the write-transaction helpers (``_exec_with_commit``,
  ``_delete_session_with_commit``, ``_refresh_last_used_with_commit``,
  ``_cleanup_expired_with_commit``, ``_try_delete_session``).
* :mod:`._validate` — the ordered :func:`validate_session` phase helpers
  (``_fetch_session_row``, ``_idle_expired``, ``_fingerprint_mismatch``,
  ``_maybe_refresh_last_used``, ``_maybe_sweep_expired``) and the
  process-wide expired-row-sweep throttle state.

Every module binds its ``logger`` to ``mediaman.web.auth.session_store``
so the package reads as one logging unit.

Re-export contract
------------------

``_SESSION_TOKEN_RE``, ``_client_fingerprint`` and ``_fingerprint_mode``
are re-exported in ``__all__`` so ``session.py`` and tests can import
them from the canonical ``session_store`` path, and monkeypatches on
``session_store._fingerprint_mode`` / ``session_store._client_fingerprint``
continue to intercept calls made inside this package. ``_hash_token`` is
re-exported for the same reason — tests assert it is the one shared
:func:`mediaman.web.auth._token_hashing.hash_token` object.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import TypedDict, cast

from mediaman.core.time import now_utc
from mediaman.core.time import parse_iso_utc as _parse_iso_aware
from mediaman.crypto import generate_session_token
from mediaman.web.auth._session_fingerprint import (
    _client_fingerprint,
    _fingerprint_mode,
)
from mediaman.web.auth._token_hashing import hash_token as _hash_token
from mediaman.web.auth.session_store._validate import (
    _fetch_session_row,
    _fingerprint_mismatch,
    _idle_expired,
    _maybe_refresh_last_used,
    _maybe_sweep_expired,
    _parse_last_used,
)

# ``_parse_iso_aware`` is now an alias for the canonical
# :func:`mediaman.core.format.parse_iso_utc`. The forensic
# ``_parse_last_used`` (in :mod:`._validate`) stays bespoke because it
# must log a warning when a stored timestamp is corrupt — a side effect
# the generic parser deliberately does not perform.

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

_HARD_EXPIRY_DAYS = 1

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


def validate_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> str | None:
    """Return the username for a valid, non-expired session token.

    Ordinary requests must not take a SQLite writer lock.
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
    # rationale: ``security_event`` swallows all exceptions internally and never
    # propagates a DB error to this call site — it is a best-effort audit helper
    # by design.  This ``except sqlite3.Error`` guard therefore protects the
    # dynamic-import / call-dispatch path only (e.g. a module-load error before
    # the function body executes).  Any such failure is logged visibly at ERROR
    # so an operator can see the audit-coverage gap in production.  The session
    # row is already deleted at this point, so re-raising would 500 a logout
    # that has in fact succeeded — catching and logging is the correct behaviour.
    # NOTE for the orchestrator: the rest of web/auth uses the transactional
    # ``security_event_or_raise`` inside ``with conn:``; making logout
    # fail-closed on audit is a larger, separately-scoped change.
    try:
        from mediaman.core.audit import security_event

        security_event(conn, event="session.destroy", actor=username, ip=ip)
    except sqlite3.Error:
        logger.exception("session.destroy audit logging failed")


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
    # back the bulk session delete that already committed, so we do not
    # re-raise; but a bulk purge leaving every reauth ticket alive is a
    # security-relevant degradation, so the failure logs at WARNING with the
    # traceback (DEBUG is off in production). ``revoke_all_reauth_for`` is a
    # single DELETE + commit, so the catch is narrowed to ``sqlite3.Error`` —
    # a non-DB exception (a bug) propagates.
    try:
        from mediaman.web.auth.reauth import revoke_all_reauth_for

        revoke_all_reauth_for(conn, username)
    except sqlite3.Error:
        logger.warning(
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
    # drift surfaces as a type-checker error rather than being silently
    # papered over by a blanket ``cast()``.  A ``cast(SessionMetadata,
    # dict(r))`` would let any column type mismatch pass mypy undetected;
    # the explicit constructor makes a type drift a compile-time break.
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
