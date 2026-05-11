"""Ring 0: canonical date/time helpers.

Single source of truth for UTC clock access and ISO-8601 parsing.
``now_iso`` was previously scattered across 20+ call sites; ``parse_iso_utc``
was previously defined in :mod:`mediaman.services.infra.format` and then
moved to :mod:`mediaman.services.infra.time` before landing here.

Ring 0 contract: stdlib only, no I/O, no imports from other mediaman modules.

Canonical home: ``mediaman.core.time``.
Back-compat shim: ``mediaman.services.infra.time``.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_utc() -> datetime:
    """Return ``datetime.now(UTC)``.

    The canonical clock surface used across mediaman; monkey-patch this
    in tests rather than reaching for the stdlib directly.  Centralising
    the clock call also means a future migration to a different timezone
    convention (UTC offset, leap-second handling, etc.) touches one line
    instead of forty.
    """
    return datetime.now(UTC)


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Equivalent to ``datetime.now(timezone.utc).isoformat()`` but defined
    once so all call sites read the same clock call and format.
    """
    return now_utc().isoformat()


def parse_iso_strict_utc(value: str | None) -> datetime | None:
    """Strict ISO-8601 parse: return ``None`` on missing/invalid input, or
    a tz-aware UTC datetime on success.

    Unlike :func:`parse_iso_utc` this rejects subtly malformed strings
    (trailing ``Z``, oversized fractional seconds, etc.) rather than
    coercing them — use it where the previous inline code path treated
    "unparseable" as "treat as expired / unknown".  Naive datetimes are
    interpreted as UTC, matching :func:`parse_iso_utc`'s convention.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string and return a timezone-aware UTC datetime.

    Tolerates the trailing ``Z`` Zulu marker and fractional-second
    suffixes longer than 6 digits (sometimes emitted by .NET clients).
    Returns ``None`` for empty or unparseable inputs.

    Naive datetimes (no offset marker) are assumed to represent UTC —
    this matches Radarr/Sonarr/Plex which emit UTC timestamps but
    sometimes omit the offset.  Callers with an authoritative non-UTC
    offset should convert before calling.
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
        dt = dt.replace(tzinfo=UTC)
    return dt
