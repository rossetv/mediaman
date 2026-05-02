"""Password re-authentication helpers.

Two distinct concepts live here:

1. :func:`require_reauth` — direct password re-check used historically by
   one-shot destructive endpoints (delete user) which want to confirm the
   password supplied in the ``X-Confirm-Password`` header *for that single
   request only*. A compromised session cookie WITHOUT the password cannot
   trigger flows guarded by this function.

2. :func:`grant_recent_reauth` / :func:`has_recent_reauth` — a short-lived,
   server-side "recent reauth" ticket bound to the caller's session token
   hash. Privilege-establishing endpoints (admin creation, sensitive
   settings, admin unlock, password change) call :func:`has_recent_reauth`
   to demand that the caller has reauthenticated within the last
   ``REAUTH_WINDOW_SECONDS`` window. The ticket is keyed on the session's
   token-hash so:

   * Routes that destroy or rotate a session call :func:`revoke_reauth`
     or :func:`revoke_all_reauth_for` so the ticket dies with the
     session. Stranded rows for sessions destroyed via other paths are
     harmless — they expire on their own and the SHA-256 token-hash
     primary key makes accidental reuse by a fresh session vanishingly
     unlikely.
   * A separate session that never reauthenticated cannot piggy-back on a
     reauth granted to a different session.
   * Restarts and multi-worker deployments share one source of truth.

   Failed reauth attempts feed the existing :mod:`mediaman.auth.login_lockout`
   counter under a ``reauth:<username>`` namespace so an attacker who steals a
   session cookie cannot mount an offline-style password oracle against the
   reauth endpoint either — the same escalating lockout that gates plain
   login also gates reauth.

The 5-minute window is the default; it can be tuned via the
``MEDIAMAN_REAUTH_WINDOW_SECONDS`` environment variable for stricter
deployments. The window is intentionally short — long enough to chain a
"prompt for password, then perform the action" flow without a second
prompt, short enough that a stolen session cookie observed once cannot be
used hours later for privilege-establishing actions.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import bcrypt

from mediaman.auth._token_hashing import hash_token as _hash_token

logger = logging.getLogger("mediaman")

#: Default lifetime of a "recent reauth" ticket. Five minutes — comfortable
#: for a "prompt then submit" UX, short enough that a leaked cookie cannot
#: be used hours later for privilege-establishing actions.
_DEFAULT_REAUTH_WINDOW_SECONDS = 300

#: Environment override so paranoid deployments can shorten the window.
_REAUTH_WINDOW_ENV = "MEDIAMAN_REAUTH_WINDOW_SECONDS"

#: Lockout namespace prefix used when feeding failed reauth attempts into
#: :mod:`mediaman.auth.login_lockout`. Keeps reauth failures separate from
#: real login failures so the username's plain-login counter is not also
#: tripped by a session-bound brute-force against reauth.
REAUTH_LOCKOUT_PREFIX = "reauth:"


def reauth_window_seconds() -> int:
    """Return the configured reauth window in seconds.

    Falls back to :data:`_DEFAULT_REAUTH_WINDOW_SECONDS` when the env var
    is unset, blank, or non-numeric. A value below 30 s is clamped up — the
    UX of a sub-30-second window is awful and there's no security benefit
    short of "session is stolen *and* the attacker is racing the user in
    the same second."
    """
    raw = os.environ.get(_REAUTH_WINDOW_ENV, "").strip()
    if not raw:
        return _DEFAULT_REAUTH_WINDOW_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_REAUTH_WINDOW_SECONDS
    if value < 30:
        return 30
    if value > 3600:
        return 3600
    return value


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the reauth_tickets table if it isn't there.

    The migration block in :mod:`mediaman.db.schema` creates it, but tests
    spinning up a fresh connection may skip migrations on legacy paths —
    keep the check cheap and idempotent.

    The table does NOT carry a SQL foreign key to ``admin_sessions``
    because ``admin_sessions`` is keyed on the raw token while this table
    is keyed on its SHA-256 hash, so a CASCADE relation is awkward to
    express.  Instead, every session-destruction site explicitly calls
    :func:`revoke_reauth` (logout, password change) or
    :func:`revoke_reauth_by_hash` (idle-expiry, fingerprint mismatch,
    expired-row sweep) so the ticket is removed in lockstep with the
    session row.  The ``expires_at`` field is a backstop for any path
    we missed; the SHA-256 primary key makes accidental reuse by a
    future session that happens to hash-collide vanishingly unlikely.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reauth_tickets (
            session_token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )


def require_reauth(conn: sqlite3.Connection, admin: str, password: str) -> bool:
    """Return True if *password* matches *admin*'s current hash.

    Used for one-shot password-confirm flows (delete user). Does NOT
    record failures into the lockout counter — call sites that want
    throttling should call :func:`verify_reauth_password` instead so an
    attacker cannot brute-force the password through this endpoint
    either.

    Kept for backwards-compatibility with the existing delete-user route.
    """
    if not password:
        return False
    from mediaman.auth.session import authenticate

    return authenticate(conn, admin, password, record_failures=False)


# Underscore alias kept for callers that imported the private name.
_require_reauth = require_reauth


def verify_reauth_password(
    conn: sqlite3.Connection,
    admin: str,
    password: str,
) -> bool:
    """Verify *password* and feed failures into a separate lockout namespace.

    On success: returns True and clears the reauth-namespace failure
    counter for *admin*.

    On failure: returns False and records the failure into
    :mod:`mediaman.auth.login_lockout` under the ``reauth:<admin>``
    namespace. This means:

    * Repeated wrong-password attempts at the reauth endpoint trip the
      same escalating 5/10/15 thresholds as the login endpoint.
    * The plain-login counter for ``admin`` is NOT bumped — otherwise an
      attacker with a session cookie could lock the legitimate user out of
      the login flow without ever knowing the password.
    * The lockout state for ``reauth:<admin>`` blocks reauth attempts
      *only*; the user can still sign in normally if their session
      expires.

    The function intentionally returns False (rather than raising) when
    the namespace is locked, so the caller renders the same generic
    "wrong password" response — the lock state is not leaked to the
    client.
    """
    from mediaman.auth.login_lockout import (
        check_lockout,
        record_failure,
        record_success,
    )
    from mediaman.auth.session import authenticate

    namespace = f"{REAUTH_LOCKOUT_PREFIX}{admin}"

    # Short-circuit if the reauth namespace is locked. We still burn a
    # bcrypt cycle so the timing of "locked" matches the wrong-password
    # path — otherwise an attacker with a stolen session cookie can
    # detect the lock state by latency alone (the locked-and-returns-
    # immediately path is ~0 ms, the unlocked-but-wrong-password path
    # is ~150 ms+ on cost-12 bcrypt).
    if check_lockout(conn, namespace):
        # The previous implementation called ``authenticate(conn, "",
        # password, ...)`` here intending to burn a bcrypt cycle, but
        # ``authenticate`` short-circuits on empty username before
        # reaching bcrypt — so the cycle was NOT actually burned. Mirror
        # the constant-time pattern used by ``change_password`` instead:
        # call ``bcrypt.checkpw`` directly against the dummy hash.
        from mediaman.auth.password_hash import (
            _get_dummy_hash,
            _prepare_bcrypt_input,
        )

        bcrypt.checkpw(_prepare_bcrypt_input(password), _get_dummy_hash())
        # Bump the counter so a sustained attack escalates the lock window.
        record_failure(conn, namespace)
        logger.warning(
            "auth.reauth_locked user=%s reason=lockout_active",
            admin,
        )
        return False

    if authenticate(conn, admin, password, record_failures=False):
        record_success(conn, namespace)
        return True

    record_failure(conn, namespace)
    return False


def grant_recent_reauth(
    conn: sqlite3.Connection,
    session_token: str,
    username: str,
    *,
    window_seconds: int | None = None,
) -> None:
    """Persist a "this session reauthenticated at T" marker.

    Keyed on ``sha256(session_token)`` so:

    * Two sessions for the same user maintain independent reauth state.
    * The plaintext token never appears in this table.
    * Session-destruction sites (logout, idle-expiry, fingerprint
      mismatch, expired-row sweep, bulk revocation on password change)
      explicitly delete the matching ticket via :func:`revoke_reauth` /
      :func:`revoke_reauth_by_hash` / :func:`revoke_all_reauth_for`,
      so the marker dies in lockstep with the session it was bound to.

    Idempotent: re-granting before the previous ticket expires extends
    the window. Callers must commit themselves so the grant lands in the
    same transaction as the calling endpoint's bookkeeping.
    """
    if not session_token or not username:
        return
    _ensure_table(conn)
    if window_seconds is None:
        window_seconds = reauth_window_seconds()
    now = _now()
    expires = now + timedelta(seconds=window_seconds)
    token_hash = _hash_token(session_token)
    conn.execute(
        """
        INSERT INTO reauth_tickets (session_token_hash, username, granted_at, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(session_token_hash) DO UPDATE SET
            granted_at = excluded.granted_at,
            expires_at = excluded.expires_at,
            username = excluded.username
        """,
        (token_hash, username, now.isoformat(), expires.isoformat()),
    )
    conn.commit()


def has_recent_reauth(
    conn: sqlite3.Connection,
    session_token: str | None,
    username: str,
    *,
    max_age_seconds: int | None = None,
) -> bool:
    """Return True when *session_token* holds a non-expired reauth ticket.

    The ticket must:

    * Exist for the SHA-256 hash of *session_token*.
    * Belong to *username* (cross-session swap protection).
    * Not be older than ``max_age_seconds`` (defaults to the configured
      reauth window). Even if a stored ticket has a longer ``expires_at``,
      callers can demand a stricter window per-call — useful for
      especially destructive actions.
    """
    if not session_token or not username:
        return False
    if max_age_seconds is None:
        max_age_seconds = reauth_window_seconds()
    _ensure_table(conn)
    token_hash = _hash_token(session_token)
    row = conn.execute(
        "SELECT username, granted_at, expires_at FROM reauth_tickets WHERE session_token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return False
    if row["username"] != username:
        return False
    now = _now()
    try:
        granted = datetime.fromisoformat(row["granted_at"])
        expires = datetime.fromisoformat(row["expires_at"])
    except (TypeError, ValueError):
        return False
    if granted.tzinfo is None:
        granted = granted.replace(tzinfo=UTC)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    # Honour both the stored expiry AND the per-call max_age clamp so a
    # caller can demand a stricter window than the original grant.
    if now > expires:
        return False
    return not now - granted > timedelta(seconds=max_age_seconds)


def revoke_reauth(conn: sqlite3.Connection, session_token: str) -> None:
    """Delete the reauth ticket for *session_token* if any.

    Called on logout, password-change, and session revocation paths so a
    session whose security context has changed cannot continue to
    perform privileged actions on the strength of an earlier reauth.
    """
    if not session_token:
        return
    revoke_reauth_by_hash(conn, _hash_token(session_token))


def revoke_reauth_by_hash(conn: sqlite3.Connection, token_hash: str) -> None:
    """Delete the reauth ticket whose key is *token_hash*.

    Used by the session validator's idle-expiry, fingerprint-mismatch,
    and expired-row sweep paths — they only have the SHA-256 hash on
    hand (the raw token is intentionally not stored in
    ``admin_sessions``).  Without this helper those paths could not
    revoke the matching reauth ticket and a leaked session cookie
    plus a freshly granted ticket could remain replayable for the
    rest of the ticket TTL after the legitimate session was killed.

    Owns its own commit — used by callers that are NOT already inside
    an open transaction (logout, password-change, etc).  Callers that
    are in a transaction (the session-store atomic-delete path) must
    use :func:`revoke_reauth_by_hash_in_tx` instead.
    """
    if not token_hash:
        return
    _ensure_table(conn)
    conn.execute(
        "DELETE FROM reauth_tickets WHERE session_token_hash = ?",
        (token_hash,),
    )
    conn.commit()


def revoke_reauth_by_hash_in_tx(conn: sqlite3.Connection, token_hash: str) -> None:
    """In-transaction variant of :func:`revoke_reauth_by_hash`.

    Identical body but does NOT call ``commit()`` — the caller is
    expected to have already opened a ``BEGIN IMMEDIATE`` and will
    issue the COMMIT itself.  Lets the session-store delete the
    session row and revoke the matching reauth ticket inside a single
    atomic transaction so a failure on either side rolls both back
    (audit finding: the previous split-transaction layout could leave
    the session deleted but the ticket alive if the reauth side
    failed).

    A no-op when *token_hash* is empty so callers do not have to
    pre-validate input.
    """
    if not token_hash:
        return
    # ``CREATE TABLE IF NOT EXISTS`` is safe to issue inside an open
    # transaction in SQLite — it becomes part of the transaction and
    # is committed by the caller.
    _ensure_table(conn)
    conn.execute(
        "DELETE FROM reauth_tickets WHERE session_token_hash = ?",
        (token_hash,),
    )


def cleanup_expired_reauth(conn: sqlite3.Connection, now_iso: str | None = None) -> int:
    """Sweep reauth tickets whose ``expires_at`` is in the past.

    Mirrors the periodic ``admin_sessions`` expired-row sweep so dead
    tickets do not pile up.  Returns the number of rows deleted.
    """
    _ensure_table(conn)
    cutoff = now_iso or _now().isoformat()
    cur = conn.execute(
        "DELETE FROM reauth_tickets WHERE expires_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount or 0


def revoke_all_reauth_for(conn: sqlite3.Connection, username: str) -> int:
    """Delete every reauth ticket belonging to *username*.

    Used by the password-change flow (the user's sessions are all
    revoked, so the tickets bound to those sessions must die too) and by
    administrative session-purge flows.
    """
    if not username:
        return 0
    _ensure_table(conn)
    cur = conn.execute(
        "DELETE FROM reauth_tickets WHERE username = ?",
        (username,),
    )
    conn.commit()
    return cur.rowcount or 0
