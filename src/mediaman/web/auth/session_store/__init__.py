"""Admin session persistence, validation, and hardening — re-export barrel.

Owns the "how are sessions
persisted and validated" concern; password hashing lives in
`mediaman.web.auth.password_hash`.

The public surface — ``create_session``,
``validate_session``, ``destroy_session``, ``end_other_session``,
``destroy_all_sessions_for``,
``list_sessions_for`` and the `SessionMetadata` projection — plus
the constants tests read directly (``_SESSION_TOKEN_RE``,
``_HARD_EXPIRY_DAYS``) stay in this barrel. The private helpers live in
three responsibility modules:

* `._writes` — the write-transaction helpers (``_exec_with_commit``,
  ``_delete_session_with_commit``, ``_refresh_last_used_with_commit``,
  ``_cleanup_expired_with_commit``, ``_try_delete_session``).
* `._validate` — the ordered `validate_session` phase helpers
  (``_fetch_session_row``, ``_idle_expired``, ``_fingerprint_mismatch``,
  ``_maybe_refresh_last_used``, ``_maybe_sweep_expired``) and the
  process-wide expired-row-sweep throttle state.
* `._device` — ``device_label``, the browser and system named by a User-Agent.

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
`mediaman.web.auth._token_hashing.hash_token` object.
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Literal, TypedDict, cast

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.crypto import generate_session_token
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth._session_fingerprint import (
    _client_fingerprint,
    _fingerprint_mode,
)
from mediaman.web.auth._token_hashing import hash_token as _hash_token
from mediaman.web.auth.session_store._device import device_label
from mediaman.web.auth.session_store._validate import (
    _fetch_session_row,
    _fingerprint_mismatch,
    _idle_expired,
    _maybe_refresh_last_used,
    _maybe_sweep_expired,
    _parse_last_used,
)
from mediaman.web.auth.session_store._writes import _try_delete_session

# The hard-expiry parse uses the STRICT parser
# (`mediaman.core.time.parse_iso_strict_utc`), matching the idle
# path (``_parse_last_used``). All session timestamps are written by this
# package via ``now.isoformat()`` (canonical ``+00:00``), so the lenient
# foreign-timestamp parser is wrong here — and treating an unparseable
# expiry as "no expiry" would fail OPEN (an immortal session). The
# forensic ``_parse_last_used`` (in `_validate`) stays bespoke
# because it must log a warning when a stored timestamp is corrupt — a
# side effect the generic parser deliberately does not perform.

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
        _sanitise_log_field(username),
        mode,
        bool(fingerprint),
    )
    # The raw token is NEVER persisted. The ``token`` PK column
    # stores the SHA-256 ``token_hash`` — the column is named ``token`` for
    # legacy-schema reasons (see the schema TODO) and ``token_hash`` is a
    # redundant duplicate kept only until the legacy PK can be renamed. Both
    # columns therefore receive the SAME hash value here; any future code
    # reading ``admin_sessions.token`` gets the hash, not the raw token.
    # Schema change is destructive and deferred — this is documentation only.
    with conn:
        conn.execute(
            "INSERT INTO admin_sessions "
            "(token, token_hash, username, created_at, expires_at, last_used_at, "
            " fingerprint, issued_ip, device_label, public_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_hash,
                token_hash,
                username,
                now_iso,
                expires_at,
                now_iso,
                fingerprint,
                client_ip or "",
                device_label(user_agent),
                secrets.token_hex(16),
            ),
        )
    logger.info("session.created user=%s ip=%s", _sanitise_log_field(username), client_ip or "-")
    return token


def validate_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    user_agent: str | None = None,
    client_ip: str | None = None,
    request_supplied: bool | None = None,
    extend: bool = True,
) -> str | None:
    """Return the username for a valid, non-expired session token.

    *extend* False leaves ``last_used_at`` as it is: nobody did anything.

    Ordinary requests must not take a SQLite writer lock.
    Validation is a read-only SELECT by default; write transactions are
    only opened when state actually changes (idle/expired delete,
    fingerprint mismatch delete, last_used_at refresh, periodic
    expired-row sweep).  This means authenticated
    requests do not block one another or compete with the
    scheduler for the WAL writer slot.

    ``request_supplied`` is the fingerprint-binding fail-closed signal.
    When ``None`` (the default) it is inferred: a caller that
    passes either ``user_agent`` or ``client_ip`` is treated as having a
    request, and a caller that passes neither is treated as the
    no-request overload — so a fingerprint-bound session can never be
    validated through the contextless path without the binding being
    enforced. Callers driven by a live request that may legitimately lack
    both fields (a request with no UA *and* no resolvable IP) should pass
    ``request_supplied=True`` explicitly. See
    `._validate._fingerprint_mismatch`.
    """
    if not token or not _SESSION_TOKEN_RE.fullmatch(token):
        return None

    now_dt = now_utc()
    now_iso = now_dt.isoformat()
    token_hash = _hash_token(token)

    row = _fetch_session_row(conn, token_hash)
    if row is None:
        return None
    # Hard-expiry check fails CLOSED: an unparseable/empty ``expires_at``
    # is treated as expired (corrupt row, half-written value, schema
    # drift) rather than as "never expires". This mirrors the idle path's
    # corrupt-timestamp handling — an immortal session is the single worst
    # outcome for the most important session control.
    expires_dt = parse_iso_strict_utc(row["expires_at"])
    if expires_dt is None or expires_dt < now_dt:
        if expires_dt is None:
            logger.info(
                "session.hard_expiry_unparseable user=%s expires_at=%r",
                _sanitise_log_field(row["username"]),
                row["expires_at"],
            )
        _try_delete_session(conn, token_hash, reason="corrupt_expiry")
        return None

    last_dt: datetime | None = _parse_last_used(row["last_used_at"], row["username"])

    if _idle_expired(conn, row, last_dt, token_hash, now_dt):
        return None
    # Infer the request signal when the caller did not pin it: passing any
    # client context implies a request was the source. Passing neither is
    # the contextless overload, which must fail closed for a bound session.
    if request_supplied is None:
        request_supplied = user_agent is not None or client_ip is not None
    if _fingerprint_mismatch(
        conn,
        row,
        token_hash,
        user_agent,
        client_ip,
        request_supplied=request_supplied,
    ):
        return None

    if extend:
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

    The username lookup, session delete, reauth-ticket delete AND the
    ``session.destroy`` audit row are wrapped in a single
    ``BEGIN IMMEDIATE`` transaction. Session revocation is a REQUIRED
    audit event, so the audit insert is now fail-closed via
    ``security_event_or_raise``: if it fails, the whole transaction rolls
    back and the session delete is undone rather than leaving an
    audit-free logout. The caller sees the ``sqlite3.Error`` and can
    surface a generic failure, but the session is NOT silently dropped
    without a trail.
    """
    # Local import to avoid the session_store -> reauth -> session
    # cycle at module load.
    from mediaman.core.audit import security_event_or_raise
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
        # Audit-in-transaction (fail-closed): a logout whose audit row is
        # lost is an audit-coverage gap for a security-relevant event.
        security_event_or_raise(conn, event="session.destroy", actor=username, ip=ip)

    logger.info("session.destroyed user=%s ip=%s", _sanitise_log_field(username or "-"), ip or "-")


def end_other_session(
    conn: sqlite3.Connection,
    username: str,
    public_id: str,
    *,
    current_token: str,
    ip: str = "",
) -> Literal["ended", "current", "unknown"]:
    """End *username*'s session *public_id*, with its reauth ticket and an audit row.

    Never the session of *current_token*. A session that has gone already and one of another
    person are both ``unknown``. The audit row is written in the same transaction, as in
    `destroy_session`.
    """
    # Local import to avoid the session_store -> reauth -> session
    # cycle at module load.
    from mediaman.core.audit import security_event_or_raise
    from mediaman.web.auth.reauth import revoke_reauth_by_hash_in_tx

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT token_hash, device_label FROM admin_sessions"
            " WHERE public_id = ? AND username = ?",
            (public_id, username),
        ).fetchone()
        if row is None:
            return "unknown"
        if row["token_hash"] == _hash_token(current_token):
            return "current"
        conn.execute("DELETE FROM admin_sessions WHERE public_id = ?", (public_id,))
        revoke_reauth_by_hash_in_tx(conn, row["token_hash"])
        security_event_or_raise(
            conn,
            event="session.revoke_one",
            actor=username,
            ip=ip,
            detail={"device": row["device_label"]} if row["device_label"] else None,
        )
    logger.info("session.revoke_one user=%s ip=%s", _sanitise_log_field(username), ip or "-")
    return "ended"


def destroy_all_sessions_for(conn: sqlite3.Connection, username: str) -> int:
    """Delete every session belonging to *username*. Returns rows affected.

    Also revokes every reauth ticket owned by *username* so a bulk session
    purge — typically driven by a forced password change or admin action —
    does not leave stale tickets that an attacker holding a related cookie
    could replay.
    """
    with conn:
        cur = conn.execute("DELETE FROM admin_sessions WHERE username = ?", (username,))
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
            "session.destroy_all: revoke_all_reauth_for failed user=%s",
            _sanitise_log_field(username),
            exc_info=True,
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
        Opaque string computed by `_client_fingerprint` at session
        creation.  Empty when fingerprint mode is ``"off"`` or no
        client context was available at creation time.
    current:
        Whether this is the session of the token the list was asked for with.
    device_label:
        Browser and system at sign-in, such as ``Safari on iPhone``; None for
        older rows.
    public_id:
        Random id the owner may end the session by; never the token or its hash.
    """

    created_at: str | None
    expires_at: str | None
    last_used_at: str | None
    issued_ip: str | None
    fingerprint: str | None
    current: bool
    device_label: str | None
    public_id: str | None


def list_sessions_for(
    conn: sqlite3.Connection, username: str, *, current_token: str | None = None
) -> list[SessionMetadata]:
    current_hash = _hash_token(current_token) if current_token else None
    rows = conn.execute(
        "SELECT created_at, expires_at, last_used_at, issued_ip, fingerprint, token_hash, "
        "device_label, public_id "
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
            current=current_hash is not None and r["token_hash"] == current_hash,
            device_label=r["device_label"],
            public_id=r["public_id"],
        )
        for r in rows
    ]
