"""Persistent per-username login lockout.

The existing rate limiter in :mod:`mediaman.auth.rate_limit` is per-IP
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
from datetime import datetime, timedelta, timezone

def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from the lockout table (stored by this module)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None

logger = logging.getLogger("mediaman")

#: Threshold → lock duration (minutes). Ordered from highest to lowest
#: so the stricter lock wins when the count crosses both.
_LOCK_RULES: tuple[tuple[int, int], ...] = (
    (15, 24 * 60),   # 15+ failures → 24 hours
    (10, 60),        # 10-14 failures → 1 hour
    (5, 15),         # 5-9 failures → 15 minutes
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
    return datetime.now(timezone.utc)


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


def check_lockout(conn: sqlite3.Connection, username: str) -> bool:
    """Return True if *username* is currently locked out.

    Does not mutate state. Intended to be called before the password
    check so a locked account short-circuits bcrypt entirely.
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
    locked_until = _parse_iso(row["locked_until"])
    if locked_until is None:
        return False
    if locked_until <= _now():
        return False
    return True


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
    """
    if not username:
        return None
    _ensure_table(conn)
    now = _now()
    now_iso = _iso(now)

    # BEGIN IMMEDIATE acquires the writer lock before reading, so the
    # read-modify-write pattern below is serialised across worker
    # threads / connections instead of racing on the counter.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # The atomic UPSERT bumps failure_count in one shot so two
        # concurrent writers cannot both observe the same pre-increment
        # value and write the same post-increment value back.
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
        row = conn.execute(
            "SELECT failure_count, first_failure_at, locked_until "
            "FROM login_failures WHERE username = ?",
            (username,),
        ).fetchone()
        count = int(row["failure_count"] or 1)
        first_failure = _parse_iso(row["first_failure_at"])
        locked_until = _parse_iso(row["locked_until"])
        currently_locked = locked_until is not None and locked_until > now

        # Decay: a streak older than 24 h that isn't currently locked is
        # stale. Reset to 1 (we just counted this attempt) and bump the
        # first_failure marker to now. A locked account never decays —
        # otherwise an attacker who waits out 24 h could reset the
        # counter by trying one more time.
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

        # Fresh row — first_failure wasn't set by the UPSERT's UPDATE
        # branch. (The INSERT branch already populated it.)
        if first_failure is None:
            first_failure = now
            conn.execute(
                "UPDATE login_failures SET first_failure_at = ? WHERE username = ?",
                (now_iso, username),
            )

        # Apply the strictest threshold hit at this count. We refresh
        # the lock on every failure at or above the 5-threshold so the
        # window slides forward under sustained attack rather than
        # sitting at a fixed "expires at 14:00" that an attacker can
        # watch tick down.
        minutes = _window_for_count(count)
        if minutes is not None:
            new_locked_until = _iso(now + timedelta(minutes=minutes))
            conn.execute(
                "UPDATE login_failures SET locked_until = ? WHERE username = ?",
                (new_locked_until, username),
            )
            logger.warning(
                "auth.account_locked user=%s count=%d minutes=%d",
                username,
                count,
                minutes,
            )
        conn.execute("COMMIT")
        return minutes
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_success(conn: sqlite3.Connection, username: str) -> None:
    """Clear the failure counter after a successful login."""
    if not username:
        return
    _ensure_table(conn)
    conn.execute(
        "DELETE FROM login_failures WHERE username = ?",
        (username,),
    )
    conn.commit()
