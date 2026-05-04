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


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Equivalent to ``datetime.now(timezone.utc).isoformat()`` but defined
    once so all call sites read the same clock call and format.
    """
    return datetime.now(UTC).isoformat()


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
