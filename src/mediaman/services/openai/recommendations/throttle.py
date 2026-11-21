"""Manual recommendation refresh throttle.

Enforces a per-site cooldown on manual refreshes so a malicious or impatient user cannot
burn OpenAI tokens by spamming the button or calling /api/recommended/refresh directly.
The scheduled background refresh is unaffected: it runs once per scan and does not
update this timestamp.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.services.infra import get_string_setting

#: Hours a manually-triggered refresh blocks further manual refreshes.
RECOMMENDATION_REFRESH_COOLDOWN_HOURS: int = 24

_LAST_REFRESH_KEY = "last_manual_recommendation_refresh"


def last_manual_refresh(conn: sqlite3.Connection) -> datetime | None:
    val = get_string_setting(conn, _LAST_REFRESH_KEY)
    if not val:
        return None
    return parse_iso_strict_utc(val)


def refresh_cooldown_remaining(conn: sqlite3.Connection) -> timedelta | None:
    last = last_manual_refresh(conn)
    if last is None:
        return None
    cooldown = timedelta(hours=RECOMMENDATION_REFRESH_COOLDOWN_HOURS)
    elapsed = now_utc() - last
    if elapsed >= cooldown:
        return None
    return cooldown - elapsed


def record_manual_refresh(conn: sqlite3.Connection, when: datetime) -> None:
    iso = when.isoformat()
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_LAST_REFRESH_KEY, iso, iso),
        )
