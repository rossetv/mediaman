"""Tiny time-helper shared by :mod:`schedule` and :mod:`summary`."""

from __future__ import annotations

import logging
from datetime import datetime

from mediaman.core.format import ensure_tz as _ensure_tz

logger = logging.getLogger("mediaman")


def _parse_days_ago(value: str | None, now: datetime) -> int | None:
    """Parse an ISO datetime string and return the number of days before *now*.

    Returns ``None`` when *value* is empty or cannot be parsed, logging a
    warning (with traceback) on parse failure so silently-wrong timestamps
    don't go unnoticed.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        dt = _ensure_tz(dt)
        return (now - dt).days
    except (ValueError, TypeError):
        logger.warning("Failed to parse days value: %r", value, exc_info=True)
        return None
