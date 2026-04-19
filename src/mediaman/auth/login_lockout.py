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

* 5 consecutive failures → account locked for 15 minutes.
* 10 consecutive failures → account locked for 1 hour. The counter is
  **not** reset by hitting the 5-failure lock; it continues climbing
  while the attacker keeps trying during the lock window.
* Successful login clears the counter and unlock timestamp.
* Decay: if 24 h has elapsed since ``first_failure_at`` and the account
  is not currently locked, the counter is reset on the next recorded
  failure (a legitimate user who mistyped once months ago doesn't stay
  at 1/5 forever).
* Lock state is **not** surfaced to the client — the caller returns
  the usual generic 401. Leaking lock state would let an attacker
  enumerate valid usernames.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("mediaman")

#: Threshold → lock duration (minutes). Ordered from highest to lowest
#: so the stricter lock wins when the count crosses both.
_LOCK_RULES: tuple[tuple[int, int], ...] = (
    (10, 60),   # 10+ failures → 1 hour
    (5, 15),    # 5-9 failures → 15 minutes
)

#: After this long with no failures, reset the counter on the next
#: recorded failure. Stops one-off typos staying on the record forever.
_DECAY_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


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


def record_failure(conn: sqlite3.Connection, username: str) -> None:
    """Record a failed login attempt for *username*.

    Increments the counter and (if a threshold is crossed) sets
    ``locked_until``. Applies decay: if ``first_failure_at`` is older
    than :data:`_DECAY_HOURS` the counter restarts at 1.
    """
    if not username:
        return
    _ensure_table(conn)
    now = _now()
    row = conn.execute(
        "SELECT failure_count, first_failure_at, locked_until "
        "FROM login_failures WHERE username = ?",
        (username,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO login_failures "
            "(username, failure_count, first_failure_at, locked_until) "
            "VALUES (?, 1, ?, NULL)",
            (username, _iso(now)),
        )
        conn.commit()
        return

    first_failure = _parse_iso(row["first_failure_at"])
    count = int(row["failure_count"] or 0)

    # Decay: a streak that started > 24 h ago and is not currently locked
    # is stale — reset. (A locked account with an old first_failure keeps
    # its counter until the lock expires, so a long attack cannot reset
    # itself by waiting out the decay window while still being locked.)
    locked_until = _parse_iso(row["locked_until"])
    currently_locked = locked_until is not None and locked_until > now
    stale = (
        first_failure is not None
        and (now - first_failure) > timedelta(hours=_DECAY_HOURS)
        and not currently_locked
    )
    if stale:
        count = 0
        first_failure = now

    count += 1
    if first_failure is None:
        first_failure = now

    # Pick the tightest lock that applies at this count.
    new_locked_until: str | None = row["locked_until"]
    for threshold, minutes in _LOCK_RULES:
        if count >= threshold:
            new_locked_until = _iso(now + timedelta(minutes=minutes))
            logger.warning(
                "auth.account_locked user=%s count=%d minutes=%d",
                username,
                count,
                minutes,
            )
            break

    conn.execute(
        "UPDATE login_failures SET failure_count = ?, "
        "first_failure_at = ?, locked_until = ? WHERE username = ?",
        (count, _iso(first_failure), new_locked_until, username),
    )
    conn.commit()


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
