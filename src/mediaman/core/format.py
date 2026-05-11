"""Ring 0: shared formatting helpers.

Replaces per-module copies of ``_format_bytes``, ``_days_ago``, and
ISO-timestamp normalisation that used to exist in at least five
different places with subtle drift.

Ring 0 contract: stdlib only (json, re, datetime), no I/O, no imports from
other mediaman modules.  :func:`parse_iso_utc` is imported from
:mod:`mediaman.core.time` — the only intra-Ring-0 import permitted.

Canonical home: ``mediaman.core.format``.
Back-compat shim: ``mediaman.services.infra.format``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime

from mediaman.core.time import now_utc
from mediaman.core.time import parse_iso_utc as parse_iso_utc

# ---------------------------------------------------------------------------
# Audit-log detail parsers — shared by dashboard and newsletter.
# ---------------------------------------------------------------------------

_AUDIT_TITLE_RE = re.compile(r"^Deleted[: ]+['\"]?(.+?)['\"]?(?:\s+by\s+.+?)?(?:\s+\[rk:.*\])?$")
_AUDIT_RK_RE = re.compile(r"\[rk:([^\]]+)\]")

#: Hard cap on the length of a string fed to :data:`_AUDIT_TITLE_RE`.
#: The non-greedy ``(.+?)`` followed by optional groups can exhibit
#: O(n^2) backtracking on long inputs, so we cap before matching to
#: keep the worst case bounded. 256 chars covers every legitimate
#: media title and a tag block by a wide margin.
_AUDIT_TITLE_MAX_INPUT = 256


def title_from_audit_detail(detail: str | None) -> str:
    """Extract a media title from an ``audit_log.detail`` string.

    Handles both formats produced by the application:

    * ``"Deleted: Some Title [rk:123]"`` — scanner engine
    * ``"Deleted 'Some Title' by admin [rk:123]"`` — library route

    Returns ``"Unknown"`` when *detail* is empty or does not match.

    Long inputs are truncated to :data:`_AUDIT_TITLE_MAX_INPUT` before
    matching to bound the regex worst case; a malformed audit row
    cannot trigger pathological backtracking.
    """
    if not detail:
        return "Unknown"
    capped = detail if len(detail) <= _AUDIT_TITLE_MAX_INPUT else detail[:_AUDIT_TITLE_MAX_INPUT]
    m = _AUDIT_TITLE_RE.match(capped)
    return m.group(1) if m else capped


def rk_from_audit_detail(detail: str | None) -> str | None:
    """Extract the ``plex_rating_key`` from an ``[rk:...]`` tag in a detail string.

    Returns ``None`` if *detail* is empty or contains no ``[rk:…]`` tag.
    """
    if not detail:
        return None
    m = _AUDIT_RK_RE.search(detail)
    return m.group(1) if m else None


def ensure_tz(dt: datetime | None) -> datetime:
    """Return *dt* in UTC, treating naive datetimes as UTC.

    Every authoritative source of datetimes in mediaman now produces
    UTC: PlexAPI's ``viewedAt`` is built with ``tz=timezone.utc``, the
    scanner uses ``datetime.now(timezone.utc).isoformat()`` for
    ``added_at``, and Radarr/Sonarr emit ISO timestamps that
    :func:`parse_iso_utc` already treats as UTC when no offset is
    present.

    The previous implementation treated a naive input as **local
    time** (via ``.astimezone(timezone.utc)``), which silently shifted
    timestamps by the local UTC offset and disagreed with
    :func:`parse_iso_utc`'s naive-as-UTC convention. Two helpers that
    both labelled themselves "ensure UTC" but applied opposite rules
    is a bug factory; the unified rule is "naive is UTC".

    A ``None`` input returns the current UTC time.
    """
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
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


#: English month names — used to keep formatted dates locale-stable
#: even when the host's ``LC_TIME`` is set to something else. ``strftime``
#: honours the system locale by default which would render the same
#: timestamp differently across hosts (e.g. "1 abr 2026" on a Spanish
#: locale, breaking deterministic output the newsletter relies on).
_ENGLISH_MONTH_FULL = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_ENGLISH_MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_day_month(dt: datetime, *, long_month: bool = False) -> str:
    """Format *dt* as a day-month-year string without the ``%-d`` platform gotcha.

    ``%-d`` (GNU strftime extension for zero-strip) works on Linux but
    raises ``ValueError`` on Windows and some BSD platforms.  This helper
    builds the day component manually and uses an internal English
    month-name table, so it is safe and locale-stable everywhere.

    The internal month table sidesteps ``strftime``'s locale awareness:
    on a host with a non-English ``LC_TIME``, ``%b`` / ``%B`` would
    render the month in the host's language and break newsletters
    rendered for English-speaking subscribers.

    Args:
        dt: A :class:`datetime` instance (aware or naive).
        long_month: When ``True``, use the full month name (e.g. ``"April"``);
            when ``False`` (default), use the abbreviated form (e.g. ``"Apr"``).

    Examples:
        ``format_day_month(dt)``          → ``"1 Apr 2026"``
        ``format_day_month(dt, long_month=True)`` → ``"1 April 2026"``
    """
    table = _ENGLISH_MONTH_FULL if long_month else _ENGLISH_MONTH_ABBR
    return f"{dt.day} {table[dt.month - 1]} {dt.year}"


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
    if not isinstance(value, (str, bytes, bytearray)):
        return []
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


def relative_day_label(
    target: datetime,
    *,
    now: datetime,
    today: str = "today",
    tomorrow: str = "tomorrow",
    future: Callable[[int], str],
    past: Callable[[int], str] | None = None,
) -> str:
    """Render *target* relative to *now* as a short English phrase.

    Three sites previously duplicated this with drifting prose; callers
    now supply only the strings.  ``future(days)`` produces the label
    for "in N days"; ``past`` (optional) for "N days ago".  When
    ``past`` is ``None``, past dates collapse to the ``today`` label
    (matching the historical "Expires today" / "Deletes today" rule
    where a passed-deadline still renders as the today phrase).
    """
    delta = (target - now).days
    if delta == 0 or (delta < 0 and past is None):
        return today
    if delta == 1:
        return tomorrow
    if delta > 0:
        return future(delta)
    # delta < 0 and past is not None — checked above.
    assert past is not None
    return past(-delta)


def days_ago(value: str | None) -> str:
    """Return a human-readable "N days ago" string for an ISO timestamp.

    Returns ``""`` for unparseable or missing inputs, ``"today"`` for
    zero-day deltas, ``"yesterday"`` for one-day deltas, otherwise
    ``"N days ago"``.
    """
    dt = parse_iso_utc(value)
    if dt is None:
        return ""
    delta = (now_utc() - dt).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "yesterday"
    return f"{delta} days ago"
