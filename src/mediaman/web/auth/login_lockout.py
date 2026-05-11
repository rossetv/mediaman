"""Persistent per-username login lockout.

The existing rate limiter in :mod:`mediaman.web.auth.rate_limit` is per-IP
and in-memory. An attacker rotating through IPs (cheap on any cloud
provider) can brute-force a known username without ever tripping it,
and restarting the process wipes the counter.

This module provides a DB-backed per-username counter so a sustained
brute-force against *any* account gets locked out regardless of source
IP and survives restarts.

Semantics
---------

* 5-9 consecutive failures → account locked for 15 minutes.
* 10-14 consecutive failures → account locked for 1 hour.
* 15+ consecutive failures → account locked for 24 hours.
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
collide on the 5 / 10 / 15 threshold. Previously the module did
``SELECT then UPDATE`` which lost increments around the threshold and
delayed (or entirely skipped) the escalation.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from mediaman.core.time import now_utc
from mediaman.core.time import parse_iso_utc as _parse_iso

logger = logging.getLogger(__name__)

#: Threshold → lock duration (minutes). Ordered from highest to lowest
#: so the stricter lock wins when the count crosses both.
_LOCK_RULES: tuple[tuple[int, int], ...] = (
    (15, 24 * 60),  # 15+ failures → 24 hours
    (10, 60),  # 10-14 failures → 1 hour
    (5, 15),  # 5-9 failures → 15 minutes
)


def _window_for_count(count: int) -> int | None:
    """Return the lock-duration in minutes for *count*, or None if no lock applies."""
    for threshold, minutes in _LOCK_RULES:
        if count >= threshold:
            return minutes
    return None


#: After this long with no failures, reset the counter on the next
#: recorded failure. Stops one-off typos staying on the record forever.
_DECAY_HOURS = 24


def _now() -> datetime:
    return now_utc()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the login_failures table if it isn't there yet.

    The v12 migration in :mod:`mediaman.db` creates this, but tests and
    legacy DBs may skip the migration — keep the check cheap and local.
    """
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


def is_locked_out(conn: sqlite3.Connection, username: str) -> bool:
    """Return True if *username* is currently locked out.

    Does not mutate state. Intended to be called before the password
    check so a locked account short-circuits bcrypt entirely.

    ``username`` is normalised to lowercase before the lookup so that
    ``Admin`` and ``admin`` share the same failure counter (M16).
    """
    if not username:
        return False
    username = username.lower()
    _ensure_table(conn)
    row = conn.execute(
        "SELECT locked_until FROM login_failures WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return False
    locked_until = _parse_iso(row["locked_until"])
    if locked_until is None:
        return False
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
    return conn.execute(
        "SELECT failure_count, first_failure_at, locked_until "
        "FROM login_failures WHERE username = ?",
        (username,),
    ).fetchone()


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
    """Backfill first_failure_at when the UPSERT UPDATE branch left it unset.

    Fresh row — first_failure wasn't set by the UPSERT's UPDATE
    branch. (The INSERT branch already populated it.)
    """
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
      forward to ``now + minutes``. This is the original behaviour
      that makes a sustained attack burn against an ever-fresh
      timer.
    * If the account IS currently locked AND the new threshold is
      STRICTER than the previous threshold this row was last
      locked at (10-failure / 15-failure escalation), promote the
      window. We compare *threshold band*, not *expiry timestamp*
      — otherwise every additional sub-threshold failure would
      slide the window forwards by a few microseconds and re-open
      the M21 DoS.
    * If the account IS currently locked AND we're still in the
      same severity band, leave ``locked_until`` alone. Otherwise
      an unauthenticated attacker can keep an admin permanently
      locked out by pinging the login endpoint forever (M21).

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
                username,
                count,
                minutes,
            )
        else:
            logger.info(
                "auth.lock_window_unchanged user=%s count=%d minutes=%d "
                "reason=already_locked_no_escalation",
                username,
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

    ``username`` is normalised to lowercase before writing so that
    ``Admin`` and ``admin`` share the same failure counter (M16).
    """
    if not username:
        return None
    username = username.lower()
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
        return _record_failure_in_tx(conn, username, now, now_iso)


def record_success(conn: sqlite3.Connection, username: str) -> None:
    """Clear the failure counter after a successful login.

    ``username`` is normalised to lowercase to match the form written by
    :func:`record_failure`.
    """
    if not username:
        return
    username = username.lower()
    _ensure_table(conn)
    conn.execute(
        "DELETE FROM login_failures WHERE username = ?",
        (username,),
    )
    conn.commit()


def admin_unlock(conn: sqlite3.Connection, username: str) -> bool:
    """Clear the failure counter and lock for *username*.

    Returns True when there was an existing record to delete, False
    when *username* was already unlocked (or never existed). Callers
    can use the return value to decide whether to log a "no-op" event.

    Does NOT commit — the caller commits inside the wider transaction
    that records the audit row, so the unlock and audit land
    atomically.
    """
    if not username:
        return False
    username = username.lower()
    _ensure_table(conn)
    cur = conn.execute(
        "DELETE FROM login_failures WHERE username = ?",
        (username,),
    )
    return (cur.rowcount or 0) > 0
