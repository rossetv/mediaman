"""Shared time helpers.

Single source of truth for the ``datetime.now(timezone.utc).isoformat()``
expression that was previously scattered across 20+ call sites. Importing
from here instead of repeating the expression ensures every module uses
the same clock resolution and format.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Equivalent to ``datetime.now(timezone.utc).isoformat()`` but defined
    once so all call sites read the same clock call and format.
    """
    return datetime.now(UTC).isoformat()
