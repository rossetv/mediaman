"""Write-transaction helpers for the session store.

Every function here opens (or wraps) a short ``BEGIN IMMEDIATE`` write
transaction against ``admin_sessions``. The validate-session fast path
in :mod:`._validate` stays read-only and only reaches for these helpers
on the rare state-changing branches (idle-expiry delete, fingerprint
mismatch delete, ``last_used_at`` refresh, periodic expired-row sweep).
The public surface (``create_session`` / ``destroy_session`` / …) lives
in the package :mod:`__init__`.

The ``logger`` is bound to ``__package__`` so every record this module
emits carries the canonical ``mediaman.web.auth.session_store`` name —
the package reads as one logging unit regardless of how its private
modules are split.
"""

from __future__ import annotations

import logging
import sqlite3

# rationale: bind to the package logger, not ``__name__`` — the session
# store is one logging unit; a record from this private module must
# still surface under ``mediaman.web.auth.session_store``.
logger = logging.getLogger(__package__)


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
    # session sweep that already ran, so we do not re-raise; but the failure
    # must stay operator-visible (DEBUG is off in production), hence WARNING
    # with the traceback. ``cleanup_expired_reauth`` is a single DELETE +
    # commit, so the catch is narrowed to ``sqlite3.Error`` — a non-DB
    # exception (a bug) propagates.
    try:
        from mediaman.web.auth.reauth import cleanup_expired_reauth

        cleanup_expired_reauth(conn, now_iso)
    except sqlite3.Error:
        logger.warning("session.cleanup: cleanup_expired_reauth failed", exc_info=True)


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
    # rationale: a transient DB failure (lock contention) on this best-effort
    # eviction write must not 500 the user during validation; log and let the
    # next request retry. The catch is narrowed to ``sqlite3.Error`` on
    # purpose — a non-DB exception (a bad import, a TypeError) is a real bug
    # on the security-critical eviction path and MUST propagate rather than
    # leave a stolen/expired session silently alive (fail closed).
    except sqlite3.Error:
        logger.warning(
            "session.delete_failed reason=%s",
            reason,
            exc_info=True,
        )
