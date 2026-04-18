"""Shared formatting helpers.

Replaces per-module copies of ``_format_bytes``, ``_days_ago``, and
ISO-timestamp normalisation that used to exist in at least five
different places with subtle drift.
"""

from __future__ import annotations

from datetime import datetime, timezone


def format_bytes(n: int | None) -> str:
    """Return a human-readable byte-count string (e.g. ``"1.3 GB"``).

    Renders floats with one decimal below 100, whole numbers above, and
    handles negative / None inputs gracefully.
    """
    if not n or n <= 0:
        return "0 B"
    for unit, threshold in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= threshold:
            value = n / threshold
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{n} B"


def parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string and return a timezone-aware UTC datetime.

    Tolerates the trailing ``Z`` Zulu marker and fractional-second
    suffixes longer than 6 digits (sometimes emitted by .NET clients).
    Returns ``None`` for empty or unparseable inputs.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Truncate sub-millisecond precision to fit Python's parser.
    if "." in s:
        head, _, rest = s.partition(".")
        # Separate the fractional-seconds part from any timezone suffix.
        tz_idx = len(rest)
        for sep in ("+", "-", "Z"):
            i = rest.find(sep)
            if i != -1:
                tz_idx = min(tz_idx, i)
        frac, suffix = rest[:tz_idx], rest[tz_idx:]
        s = f"{head}.{frac[:6]}{suffix}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_ago(value: str | None) -> str:
    """Return a human-readable "N days ago" string for an ISO timestamp.

    Returns ``""`` for unparseable or missing inputs, ``"today"`` for
    zero-day deltas, ``"yesterday"`` for one-day deltas, otherwise
    ``"N days ago"``.
    """
    dt = parse_iso_utc(value)
    if dt is None:
        return ""
    delta = (datetime.now(timezone.utc) - dt).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "yesterday"
    return f"{delta} days ago"
