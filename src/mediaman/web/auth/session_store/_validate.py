"""Validate-session phase helpers.

:func:`mediaman.web.auth.session_store.validate_session` is on every
authenticated request, so it is decomposed into ordered phases: read the
row, check idle expiry, check the fingerprint, refresh ``last_used_at``,
and opportunistically sweep expired rows. Each phase is one function
here; the public ``validate_session`` in the package :mod:`__init__`
calls them in order and is the only caller.

Phases 1-3 are read-only by default — only the idle/mismatch eviction
branches reach for a writer lock, via :mod:`._writes`. Phases 4-5 are
the throttled write paths.

The expired-row sweep throttle (``_last_cleanup_at`` and friends) lives
here because :func:`_maybe_sweep_expired` is its only reader and writer;
it is process-wide module-level state guarded by ``_cleanup_lock`` per
§8.5. The ``logger`` is bound to ``__package__`` so records carry the
canonical ``mediaman.web.auth.session_store`` name.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import cast

from mediaman.core.time import parse_iso_strict_utc
from mediaman.web.auth._session_fingerprint import (
    _client_fingerprint,
    _fingerprint_mode,
)
from mediaman.web.auth.session_store._writes import (
    _cleanup_expired_with_commit,
    _refresh_last_used_with_commit,
    _try_delete_session,
)

# rationale: bind to the package logger, not ``__name__`` — the session
# store is one logging unit; a record from this private module must
# still surface under ``mediaman.web.auth.session_store``.
logger = logging.getLogger(__package__)

# Rate-limit state: tracks when the last expired-session sweep ran so consecutive
# requests on the same process only trigger a write transaction at most once per
# minute — kept global because the sweep is process-wide, not request-scoped.
_EXPIRED_CLEANUP_INTERVAL = 60.0
_last_cleanup_at = 0.0
_cleanup_lock = threading.Lock()

_SESSION_REFRESH_MIN_INTERVAL = timedelta(seconds=60)

_IDLE_TIMEOUT_HOURS = 24


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
    # request because the timestamp didn't update. Narrowed to
    # ``sqlite3.Error`` for consistency with the other write helpers — a
    # non-DB exception is a bug and propagates.
    except sqlite3.Error:
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
