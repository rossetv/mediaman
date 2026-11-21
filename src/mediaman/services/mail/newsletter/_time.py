"""Tiny time-helper shared by schedule and summary."""

from __future__ import annotations

import logging
from datetime import datetime

from mediaman.core.time import parse_iso_strict_utc

logger = logging.getLogger(__name__)


def _parse_days_ago(value: str | None, now: datetime) -> int | None:
    """Return the days before *now* for an ISO datetime string, or None when empty or
    unparseable (logged with a traceback so bad timestamps do not go unnoticed).
    """
    if not value:
        return None
    dt = parse_iso_strict_utc(str(value))
    if dt is None:
        logger.warning("Failed to parse days value: %r", value)
        return None
    return (now - dt).days
