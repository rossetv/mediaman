"""Tiny time-helper shared by :mod:`schedule` and :mod:`summary`."""

from __future__ import annotations

import logging
from datetime import datetime

from mediaman.core.time import parse_iso_strict_utc

logger = logging.getLogger(__name__)


def _parse_days_ago(value: str | None, now: datetime) -> int | None:
    """Parse an ISO datetime string and return the number of days before *now*.

    Returns ``None`` when *value* is empty or cannot be parsed, logging a
    warning (with traceback) on parse failure so silently-wrong timestamps
    don't go unnoticed.
    """
    if not value:
        return None
    dt = parse_iso_strict_utc(str(value))
    if dt is None:
        logger.warning("Failed to parse days value: %r", value)
        return None
    return (now - dt).days
