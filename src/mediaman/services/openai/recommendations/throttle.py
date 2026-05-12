"""Manual recommendation refresh throttle.

Enforces a per-site cooldown on manual recommendation refreshes so a
malicious or impatient user cannot burn through OpenAI tokens by
spamming the button (or by calling ``/api/recommended/refresh``
directly). The scheduled background refresh is unaffected — it runs
once per scan and does not update this timestamp.

Public API
----------
- :data:`RECOMMENDATION_REFRESH_COOLDOWN_HOURS` — configurable cooldown window.
- :func:`last_manual_refresh` — read the stored timestamp from the DB.
- :func:`refresh_cooldown_remaining` — time left on the cooldown, or ``None``.
- :func:`record_manual_refresh` — persist the start time of a new refresh.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.services.infra.settings_reader import get_string_setting

#: Hours a manually-triggered refresh blocks further manual refreshes.
RECOMMENDATION_REFRESH_COOLDOWN_HOURS: int = 24

_LAST_REFRESH_KEY = "last_manual_recommendation_refresh"


def last_manual_refresh(conn: sqlite3.Connection) -> datetime | None:
    """Return the UTC datetime of the last manual refresh, or ``None`` if none recorded."""
    val = get_string_setting(conn, _LAST_REFRESH_KEY)
    if not val:
        return None
    return parse_iso_strict_utc(val)


def refresh_cooldown_remaining(conn: sqlite3.Connection) -> timedelta | None:
    """Return time still on the manual-refresh cooldown, or ``None`` if a new run is allowed."""
    last = last_manual_refresh(conn)
    if last is None:
        return None
    cooldown = timedelta(hours=RECOMMENDATION_REFRESH_COOLDOWN_HOURS)
    elapsed = now_utc() - last
    if elapsed >= cooldown:
        return None
    return cooldown - elapsed


def record_manual_refresh(conn: sqlite3.Connection, when: datetime) -> None:
    """Persist *when* as the timestamp of the latest manual refresh.

    Uses an upsert so the value is always current regardless of whether a
    previous record exists. Commits the connection.

    Args:
        conn: Open SQLite connection with write access.
        when: UTC datetime to store (typically ``datetime.now(timezone.utc)``).
    """
    iso = when.isoformat()
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_LAST_REFRESH_KEY, iso, iso),
    )
    conn.commit()
