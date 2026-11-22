# rationale: one cohesive security state machine — every function is part of
# a single lockout protocol (table bootstrap, counter UPSERT, decay, escalation,
# admin unlock + audit).  Splitting by layer (DDL / DML / public API) would
# fragment the protocol without adding a navigable concept boundary; the file
# owns exactly one concept.
"""Persistent login lockout, per username and client IP.

The existing rate limiter in `mediaman.services.rate_limit` is per-IP
and in-memory: restarting the process wipes it.

This module keeps a DB-backed failure counter per username *and* client
IP (the full IPv4 address, or the /64 prefix for IPv6), so a sustained
brute-force from one source is locked out and the lock survives
restarts. Keying on the IP as well means wrong guesses from one address
lock out only that address: someone who knows a username cannot lock
its owner out from everywhere else. Admin unlock and a password proved
elsewhere act on every one of a user's per-IP counters. Behind a reverse
proxy that is not listed as trusted, every client shares the proxy's IP
and the lockout is in effect per username.

Semantics
---------

* 5-9 consecutive failures → username locked from that IP for 15 minutes.
* 10-14 consecutive failures → locked from that IP for 1 hour.
* 15+ consecutive failures → locked from that IP for 24 hours.
  The counter is **not** reset by hitting any threshold; it continues
  climbing while the attacker keeps trying during the lock window,
  which is what makes the escalation reachable.
* Successful login clears the counter and unlock timestamp.
* Decay: if 24 h has elapsed since ``first_failure_at`` and the account
  is not currently locked, the counter is reset on the next recorded
  failure (a legitimate user who mistyped once months ago doesn't stay
  at 1/5 forever).
* Lock state is **not** surfaced to the client — the caller returns
  the usual generic 401. Leaking lock state would let an attacker
  enumerate valid usernames.
* Once an account is locked, subsequent failures still bump the counter
  (so the 5/10/15 escalation remains reachable) but do NOT slide the
  ``locked_until`` window forwards. Otherwise an unauthenticated
  attacker can keep an admin permanently locked out by pinging the
  login endpoint in a loop — denial of service against the operator.
  An admin unlock path (``POST /api/users/{id}/unlock``) lets a fellow
  admin clear the lock once they've reauthenticated.

Concurrency
-----------

``record_failure`` uses ``INSERT ... ON CONFLICT DO UPDATE`` inside a
``BEGIN IMMEDIATE`` transaction so two concurrent failed logins from
different worker threads cannot race on the read-modify-write and
collide on the 5 / 10 / 15 threshold; a ``SELECT then UPDATE`` would lose
increments and delay (or skip) the escalation.
"""

from __future__ import annotations

import ipaddress
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import NamedTuple, cast

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.core.time import parse_iso_utc as _parse_iso
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field

logger = logging.getLogger(__name__)

#: Threshold → lock duration (minutes). Ordered from highest to lowest
#: so the stricter lock wins when the count crosses both.
_LOCK_RULES: tuple[tuple[int, int], ...] = (
    (15, 24 * 60),  # 15+ failures → 24 hours
    (10, 60),  # 10-14 failures → 1 hour
    (5, 15),  # 5-9 failures → 15 minutes
)


def _window_for_count(count: int) -> int | None:
    for threshold, minutes in _LOCK_RULES:
        if count >= threshold:
            return minutes
    return None


#: Every counter key that is not a plain login key starts with this mark.
#: Usernames have no character rule, so `login_key` escapes the rare
#: username that starts with it: no username can produce another
#: namespace's key.
_NAMESPACE_MARK = "\x1f"

#: Prefix of the re-auth failure counter keys (see `reauth_key`).
REAUTH_LOCKOUT_PREFIX = f"{_NAMESPACE_MARK}reauth:"

_ESCAPED_LOGIN_PREFIX = f"{_NAMESPACE_MARK}login:"


def login_key(username: str) -> str:
    """Return the login failure counter key for *username*.

    The username itself, unless it starts with the namespace mark.
    """
    if username.startswith(_NAMESPACE_MARK):
        return f"{_ESCAPED_LOGIN_PREFIX}{username}"
    return username


def reauth_key(username: str) -> str:
    """Return the re-auth failure counter key for *username*."""
    return f"{REAUTH_LOCKOUT_PREFIX}{username}"


_LOGIN_IP_PREFIX = f"{_NAMESPACE_MARK}login-ip:"


def _ip_bucket(client_ip: str) -> str:
    """Return the part of *client_ip* a login counter is kept for.

    The full IPv4 address, or the /64 prefix for IPv6: one IPv6 host is
    usually handed a whole /64, so a per-address key would let it step
    past its own lock. Never contains the namespace mark.
    """
    try:
        addr = ipaddress.ip_address(client_ip.strip())
    except ValueError:
        return client_ip.replace(_NAMESPACE_MARK, "")
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(addr) >> 64 << 64, 64)))
    return str(addr)


def login_ip_key(username: str, client_ip: str) -> str:
    """Return the login failure counter key for *username* from *client_ip*."""
    return f"{_LOGIN_IP_PREFIX}{_ip_bucket(client_ip)}{_NAMESPACE_MARK}{username}"


def _user_login_keys(conn: sqlite3.Connection, username: str) -> list[str]:
    """Return every stored login counter key of *username*, across all IPs.

    Includes the username-only key older rows were stored under. Matched
    by splitting each key in Python rather than with ``LIKE``, whose
    wildcards a username may contain.
    """
    rows = conn.execute(
        "SELECT username FROM login_failures WHERE substr(username, 1, ?) = ?",
        (len(_LOGIN_IP_PREFIX), _LOGIN_IP_PREFIX),
    ).fetchall()
    keys = [
        row["username"]
        for row in rows
        if row["username"][len(_LOGIN_IP_PREFIX) :].partition(_NAMESPACE_MARK)[2] == username
    ]
    keys.append(login_key(username))
    return keys


#: After this long with no failures, reset the counter on the next
#: recorded failure. Stops one-off typos staying on the record forever.
_DECAY_HOURS = 24


def _now() -> datetime:
    return now_utc()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


#: Per-connection guard so the ``CREATE TABLE IF NOT EXISTS`` backstop
#: runs at most ONCE per distinct connection object instead of on every
#: auth operation. Production schema is created by ``init_db``;
#: this DDL is only a backstop for tests / legacy DBs that
#: spin up a bare connection without running migrations. ``id(conn)`` is
#: stable for the lifetime of the connection and the set is bounded by
#: the small number of live connections, so this never leaks meaningfully.
_ENSURED_TABLE_CONN_IDS: set[int] = set()


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the login_failures table if it isn't there yet (once per conn).

    The migration in `mediaman.db` creates this in production, but
    tests and legacy DBs may skip the migration — so keep a cheap local
    backstop. To avoid re-issuing a write-locking ``CREATE TABLE`` DDL on
    every authenticated request, the DDL runs at most once per
    distinct connection.
    """
    if id(conn) in _ENSURED_TABLE_CONN_IDS:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_failures (
            username TEXT PRIMARY KEY,
            failure_count INTEGER NOT NULL DEFAULT 0,
            first_failure_at TEXT,
            locked_until TEXT
        )
        """
    )
    _ENSURED_TABLE_CONN_IDS.add(id(conn))


def is_locked_out(conn: sqlite3.Connection, username: str) -> bool:
    """Return True if *username* is currently locked out.

    Does not mutate state. Intended to be called before the password
    check so a locked account short-circuits bcrypt entirely.

    The *username* is used verbatim as the lockout key — NOT lowercased.
    ``admin_users.username`` is a case-sensitive column (no ``NOCASE``
    collation), so the credential lookup in ``_verify_credentials_against_db``
    matches case-sensitively. Lowercasing the lockout key here would
    desynchronise the two: ``Admin`` and ``admin`` would share one
    counter but be two distinct rows, enabling a cross-case lockout-DoS
    (lock ``Admin`` to lock out the real ``admin``) and a success-path
    counter mismatch. Keep the lockout key identical to the authentication
    boundary's identity.
    """
    if not username:
        return False
    _ensure_table(conn)
    row = conn.execute(
        "SELECT locked_until FROM login_failures WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return False
    raw_locked_until = row["locked_until"]
    # A NULL/empty ``locked_until`` is the legitimate "counted failures
    # but no active lock" state — not locked.
    if not raw_locked_until:
        return False
    # Strict parse + fail-closed: a NON-EMPTY but unparseable
    # ``locked_until`` is treated as STILL LOCKED, consistent with the
    # session store. An attacker who could corrupt the stored value
    # must not be able to escape the lock by garbling it.
    locked_until = parse_iso_strict_utc(raw_locked_until)
    if locked_until is None:
        logger.warning(
            "auth.lockout_corrupt_locked_until user=%s locked_until=%r reason=fail_closed",
            _sanitise_log_field(username),
            raw_locked_until,
        )
        return True
    return not locked_until <= _now()


def _update_failure_row(
    conn: sqlite3.Connection,
    username: str,
    now_iso: str,
) -> sqlite3.Row:
    """UPSERT the failure counter and return the updated row.

    The atomic UPSERT bumps failure_count in one shot so two
    concurrent writers cannot both observe the same pre-increment
    value and write the same post-increment value back.
    """
    conn.execute(
        """
        INSERT INTO login_failures
            (username, failure_count, first_failure_at, locked_until)
        VALUES (?, 1, ?, NULL)
        ON CONFLICT(username) DO UPDATE SET
            failure_count = failure_count + 1
        """,
        (username, now_iso),
    )
    return cast(
        sqlite3.Row,
        conn.execute(
            "SELECT failure_count, first_failure_at, locked_until "
            "FROM login_failures WHERE username = ?",
            (username,),
        ).fetchone(),
    )


def _apply_decay(
    conn: sqlite3.Connection,
    username: str,
    now: datetime,
    now_iso: str,
    count: int,
    first_failure: datetime | None,
    currently_locked: bool,
) -> tuple[int, datetime | None]:
    """Reset the counter if the streak is stale and the account is unlocked.

    Decay: a streak older than 24 h that isn't currently locked is
    stale. Reset to 1 (we just counted this attempt) and bump the
    first_failure marker to now. A locked account never decays —
    otherwise an attacker who waits out 24 h could reset the
    counter by trying one more time.

    Returns the (possibly updated) count and first_failure values.
    """
    if (
        first_failure is not None
        and (now - first_failure) > timedelta(hours=_DECAY_HOURS)
        and not currently_locked
    ):
        count = 1
        first_failure = now
        conn.execute(
            "UPDATE login_failures "
            "SET failure_count = 1, first_failure_at = ?, locked_until = NULL "
            "WHERE username = ?",
            (now_iso, username),
        )
    return count, first_failure


def _ensure_first_failure(
    conn: sqlite3.Connection,
    username: str,
    now: datetime,
    now_iso: str,
    first_failure: datetime | None,
) -> datetime:
    if first_failure is None:
        conn.execute(
            "UPDATE login_failures SET first_failure_at = ? WHERE username = ?",
            (now_iso, username),
        )
        return now
    return first_failure


def _apply_lockout(
    conn: sqlite3.Connection,
    username: str,
    now: datetime,
    count: int,
    currently_locked: bool,
) -> int | None:
    """Write the lockout row and log if a threshold is crossed.

    Apply the strictest threshold hit at this count.

    Window-extension policy:

    * If the account is NOT currently locked, slide the window
      forward to ``now + minutes``. This makes a sustained attack burn against
      an ever-fresh timer.
    * If the account IS currently locked AND the new threshold is
      STRICTER than the previous threshold this row was last
      locked at (10-failure / 15-failure escalation), promote the
      window. We compare *threshold band*, not *expiry timestamp*
      — otherwise every additional sub-threshold failure would
      slide the window forwards by a few microseconds, and a sustained
      attacker could keep the account locked out indefinitely.
    * If the account IS currently locked AND we're still in the
      same severity band, leave ``locked_until`` alone. Otherwise
      an unauthenticated attacker can keep an admin permanently
      locked out by pinging the login endpoint in a loop.

    The counter still climbs (so the 10 / 15 thresholds remain
    reachable) — we only refuse to slide an existing window
    forwards on bad attempts that don't escalate the severity.
    """
    minutes = _window_for_count(count)
    if minutes is not None:
        # Derive the previous threshold band from the count BEFORE
        # this attempt, so the very first failure that crosses a
        # threshold (5, 10, 15) is recognised as an escalation
        # rather than a same-band re-lock.
        previous_minutes = _window_for_count(count - 1)
        should_promote = (
            not currently_locked or previous_minutes is None or minutes > previous_minutes
        )
        if should_promote:
            conn.execute(
                "UPDATE login_failures SET locked_until = ? WHERE username = ?",
                (_iso(now + timedelta(minutes=minutes)), username),
            )
            logger.warning(
                "auth.account_locked user=%s count=%d minutes=%d",
                _sanitise_log_field(username),
                count,
                minutes,
            )
        else:
            logger.info(
                "auth.lock_window_unchanged user=%s count=%d minutes=%d "
                "reason=already_locked_no_escalation",
                _sanitise_log_field(username),
                count,
                minutes,
            )
    return minutes


def _record_failure_in_tx(
    conn: sqlite3.Connection,
    username: str,
    now: datetime,
    now_iso: str,
) -> int | None:
    """Orchestrate the failure-recording steps inside an open transaction.

    Assumes ``BEGIN IMMEDIATE`` has already been issued by the caller so
    the read-modify-write is serialised across worker threads.
    """
    row = _update_failure_row(conn, username, now_iso)
    count = int(row["failure_count"] or 1)
    first_failure = _parse_iso(row["first_failure_at"])
    locked_until = _parse_iso(row["locked_until"])
    currently_locked = locked_until is not None and locked_until > now

    count, first_failure = _apply_decay(
        conn, username, now, now_iso, count, first_failure, currently_locked
    )
    _ensure_first_failure(conn, username, now, now_iso, first_failure)
    return _apply_lockout(conn, username, now, count, currently_locked)


#: Minimum seconds between prunes started from `record_failure`.
_PRUNE_INTERVAL = 60.0
_last_prune_at = float("-inf")
_prune_lock = threading.Lock()


def _maybe_prune_stale_failures(conn: sqlite3.Connection) -> None:
    """Prune stale failure rows at most once per `_PRUNE_INTERVAL`, never raising.

    Failed logins for unknown usernames add rows with no session in play,
    so the session sweep alone cannot bound the table. A prune error must
    not break the login attempt whose failure was just recorded.
    """
    global _last_prune_at
    if time.monotonic() - _last_prune_at < _PRUNE_INTERVAL:
        return
    with _prune_lock:
        mono = time.monotonic()
        if mono - _last_prune_at < _PRUNE_INTERVAL:
            return
        _last_prune_at = mono
    try:
        prune_stale_failures(conn)
    except Exception:
        logger.warning("auth.lockout_prune_failed", exc_info=True)


def record_failure(conn: sqlite3.Connection, username: str) -> int | None:
    """Record a failed login attempt for *username*.

    Atomically increments the counter inside a ``BEGIN IMMEDIATE``
    transaction and applies the escalating lockout window (5 / 10 / 15
    thresholds → 15 min / 1 h / 24 h). Applies the 24 h decay for stale
    streaks that are not currently locked.

    Returns the lock-duration in minutes that now applies, or ``None``
    when no threshold is crossed. Callers can use this to surface a
    429 + ``Retry-After`` without re-querying the DB (lock state is
    still not exposed to the HTTP client — the caller translates the
    duration into a generic retry window).

    The *username* is used verbatim as the lockout key — NOT lowercased —
    so the counter aligns with the case-sensitive ``admin_users.username``
    lookup at the authentication boundary (see `is_locked_out`).
    """
    if not username:
        return None
    _ensure_table(conn)
    now = _now()
    now_iso = _iso(now)

    # BEGIN IMMEDIATE acquires the writer lock before reading, so the
    # read-modify-write pattern below is serialised across worker
    # threads / connections instead of racing on the counter.
    # ``with conn:`` commits on normal exit and rolls back on exception;
    # the explicit BEGIN IMMEDIATE here preserves the write-lock semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        minutes = _record_failure_in_tx(conn, username, now, now_iso)
    _maybe_prune_stale_failures(conn)
    return minutes


def record_success(conn: sqlite3.Connection, username: str) -> None:
    """Clear the failure counter after a successful login.

    The *username* is used verbatim — matching the key written by
    `record_failure` and looked up by `is_locked_out`.
    """
    if not username:
        return
    _ensure_table(conn)
    conn.execute(
        "DELETE FROM login_failures WHERE username = ?",
        (username,),
    )
    conn.commit()


def clear_unlocked_login_failures(conn: sqlite3.Connection, username: str) -> None:
    """Clear *username*'s login failure counters, from every IP, that hold no lock.

    For a password proved outside the login form (re-auth, password
    change): it ends a streak of typos, but must not lift a lock that
    someone else's guesses set.
    """
    _ensure_table(conn)
    # The write lock is taken before the check, so a lock set by a
    # concurrent failure cannot land between the check and the delete.
    # ``is_locked_out`` is the one definition of "locked", corrupt
    # ``locked_until`` values included (those stay locked).
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for key in _user_login_keys(conn, username):
            if not is_locked_out(conn, key):
                conn.execute("DELETE FROM login_failures WHERE username = ?", (key,))


def prune_stale_failures(conn: sqlite3.Connection) -> int:
    """Delete failure rows that no longer count and hold no active lock.

    A row whose streak began more than `_DECAY_HOURS` ago and whose lock
    (if any) has ended is one the next failure would reset to 1 anyway,
    so deleting it changes nothing the lockout counts. A row with an
    active lock, or one whose ``locked_until`` cannot be parsed (fail
    closed, as in `is_locked_out`), is never deleted. Returns the
    number of rows deleted. Commits.
    """
    _ensure_table(conn)
    now = _now()
    cutoff = _iso(now - timedelta(hours=_DECAY_HOURS))
    with conn:
        # The write lock is taken first, so a lock set by a concurrent
        # failure cannot land between the check and the delete.
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            "DELETE FROM login_failures WHERE first_failure_at < ? "
            "AND (locked_until IS NULL OR locked_until = '')",
            (cutoff,),
        ).rowcount
        for row in conn.execute(
            "SELECT username, locked_until FROM login_failures "
            "WHERE first_failure_at < ? AND locked_until != ''",
            (cutoff,),
        ).fetchall():
            locked_until = parse_iso_strict_utc(row["locked_until"])
            if locked_until is not None and locked_until <= now:
                conn.execute("DELETE FROM login_failures WHERE username = ?", (row["username"],))
                deleted += 1
    return deleted or 0


def admin_unlock(conn: sqlite3.Connection, username: str) -> bool:
    """Clear the login (from every IP) and re-auth failure counters and locks for *username*.

    Returns True when there was an existing record to delete, False
    when *username* was already unlocked (or never existed). Callers
    can use the return value to decide whether to log a "no-op" event.

    Does NOT commit — the caller commits inside the wider transaction
    that records the audit row, so the unlock and audit land
    atomically.
    """
    if not username:
        return False
    _ensure_table(conn)
    keys = [*_user_login_keys(conn, username), reauth_key(username)]
    cur = conn.executemany(
        "DELETE FROM login_failures WHERE username = ?",
        [(key,) for key in keys],
    )
    return (cur.rowcount or 0) > 0


class UnlockResult(NamedTuple):
    """Outcome of `admin_unlock_with_audit`."""

    #: A login or re-auth lock was in force (not merely a failure count).
    was_locked: bool
    #: Any failure record existed and was deleted.
    cleared: bool


def admin_unlock_with_audit(
    conn: sqlite3.Connection,
    target_username: str,
    *,
    audit_actor: str,
    audit_ip: str = "",
    target_id: int,
) -> UnlockResult:
    """Clear *target_username*'s lock and audit-log the unlock atomically.

    Wraps `admin_unlock` and `security_event_or_raise` in a
    single ``BEGIN IMMEDIATE`` block so the unlock and the audit row
    land together — if the audit insert fails, the unlock rolls back.
    The result tells the caller whether a lock was in force and whether
    any failure record was cleared; the audit row's ``had_lock`` is the
    former.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_table(conn)
        was_locked = any(
            is_locked_out(conn, key)
            for key in [*_user_login_keys(conn, target_username), reauth_key(target_username)]
        )
        cleared = admin_unlock(conn, target_username)
        from mediaman.core.audit import security_event_or_raise

        security_event_or_raise(
            conn,
            event="user.unlocked",
            actor=audit_actor,
            ip=audit_ip,
            detail={
                "target_id": target_id,
                "target_username": target_username,
                "had_lock": was_locked,
            },
        )
    return UnlockResult(was_locked, cleared)
