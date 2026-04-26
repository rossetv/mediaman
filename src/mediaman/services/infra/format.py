"""Shared formatting helpers.

Replaces per-module copies of ``_format_bytes``, ``_days_ago``, and
ISO-timestamp normalisation that used to exist in at least five
different places with subtle drift.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Audit-log detail parsers — shared by dashboard and newsletter.
# ---------------------------------------------------------------------------

_AUDIT_TITLE_RE = re.compile(r"^Deleted[: ]+['\"]?(.+?)['\"]?(?:\s+by\s+.+?)?(?:\s+\[rk:.*\])?$")
_AUDIT_RK_RE = re.compile(r"\[rk:([^\]]+)\]")


def title_from_audit_detail(detail: str | None) -> str:
    """Extract a media title from an ``audit_log.detail`` string.

    Handles both formats produced by the application:

    * ``"Deleted: Some Title [rk:123]"`` — scanner engine
    * ``"Deleted 'Some Title' by admin [rk:123]"`` — library route

    Returns ``"Unknown"`` when *detail* is empty or does not match.
    """
    if not detail:
        return "Unknown"
    m = _AUDIT_TITLE_RE.match(detail)
    return m.group(1) if m else detail


def rk_from_audit_detail(detail: str | None) -> str | None:
    """Extract the ``plex_rating_key`` from an ``[rk:...]`` tag in a detail string.

    Returns ``None`` if *detail* is empty or contains no ``[rk:…]`` tag.
    """
    if not detail:
        return None
    m = _AUDIT_RK_RE.search(detail)
    return m.group(1) if m else None


def ensure_tz(dt: datetime | None) -> datetime:
    """Return *dt* in UTC, treating naive datetimes as local time.

    PlexAPI returns naive datetimes via ``datetime.fromtimestamp()``,
    which produces **local** time. Using ``.replace(tzinfo=UTC)``
    would mislabel the local time as UTC — off by the local UTC offset.
    ``.astimezone(UTC)`` correctly converts from local to UTC.

    A ``None`` input returns the current UTC time.
    """
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.astimezone(timezone.utc)
    return dt


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
        # Naive datetimes are assumed to represent UTC — this matches the
        # behaviour of Radarr/Sonarr/Plex which emit UTC timestamps but
        # sometimes omit the offset marker.  Callers that have an
        # authoritative non-UTC offset should convert before calling.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_day_month(dt: "datetime", *, long_month: bool = False) -> str:
    """Format *dt* as a day-month-year string without the ``%-d`` platform gotcha.

    ``%-d`` (GNU strftime extension for zero-strip) works on Linux but
    raises ``ValueError`` on Windows and some BSD platforms.  This helper
    uses ``%d`` and then strips the leading zero manually, so it is safe
    everywhere.

    Args:
        dt: A :class:`datetime` instance (aware or naive).
        long_month: When ``True``, use the full month name (e.g. ``"April"``);
            when ``False`` (default), use the abbreviated form (e.g. ``"Apr"``).

    Examples:
        ``format_day_month(dt)``          → ``"1 Apr 2026"``
        ``format_day_month(dt, long_month=True)`` → ``"1 April 2026"``
    """
    fmt = "%d %B %Y" if long_month else "%d %b %Y"
    s = dt.strftime(fmt)
    # Strip a leading zero from the day component (e.g. "01" → "1").
    if s and s[0] == "0":
        s = s[1:]
    return s


def safe_json_list(value: object) -> list:
    """Parse *value* as JSON and return a list, or ``[]`` on any failure.

    Handles the repeated pattern of ``json.loads(genres_or_cast or "[]")``
    with a try/except that was copy-pasted across six call sites.

    Args:
        value: A JSON string, an already-parsed list, or any falsy value.

    Returns:
        The parsed list, or ``[]`` when *value* is falsy or invalid JSON.
    """
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def normalise_media_type(raw: str | None) -> str:
    """Normalise a raw media-type string to ``"movie"``, ``"tv"``, or ``"anime"``.

    Returns ``"movie"`` for any unrecognised or empty input.
    """
    if not raw:
        return "movie"
    lower = raw.strip().lower()
    if lower in ("tv", "show", "series"):
        return "tv"
    if lower == "anime":
        return "anime"
    return "movie"


def media_type_badge(media_type: str | None) -> tuple[str, str]:
    """Return ``(badge_class, type_label)`` for a media-type string.

    Encapsulates the ``{"movie": "badge-movie", ...}.get(...)`` dict that
    was repeated in kept.py, dashboard.py, and history.py.

    Returns:
        A tuple ``(badge_class, type_label)`` where both are non-empty strings.
    """
    mt = normalise_media_type(media_type)
    badge = {"movie": "badge-movie", "tv": "badge-tv", "anime": "badge-anime"}.get(
        mt, "badge-movie"
    )
    return badge, mt.upper()


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
