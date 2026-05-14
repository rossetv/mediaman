"""Human-readable display formatters for ``scheduled_actions`` values.

Pure functions that convert stored DB values to display strings — no DB
access, no side effects.  Kept in a dedicated module so the keep-page
templates can import only the display layer without pulling in the DB
and token machinery.
"""

from __future__ import annotations

from mediaman.core.format import format_day_month, relative_day_label
from mediaman.core.scheduled_action_kinds import ACTION_PROTECTED_FOREVER
from mediaman.core.time import now_utc, parse_iso_strict_utc


def format_expiry(action: str | None, execute_at: str | None) -> str:
    """Return a human-readable expiry string for a protected item.

    * ``"Forever"`` for ``protected_forever``.
    * ``"Expires today"`` / ``"Expires tomorrow"`` / ``"Expires in N days"``
      for snoozed items with a parseable future deadline.
    * ``"Unknown"`` for missing or unparseable deadlines.
    """
    if action == ACTION_PROTECTED_FOREVER:
        return "Forever"
    dt = parse_iso_strict_utc(execute_at)
    if dt is None:
        return "Unknown"
    return relative_day_label(
        dt,
        now=now_utc(),
        today="Expires today",
        tomorrow="Expires tomorrow",
        future=lambda days: f"Expires in {days} days",
    )


def format_added_display(raw_added: object) -> str:
    """Format a stored ``added_at`` value for display on the keep page.

    Renders as ``"5 May 2026"``-style text via the platform-safe
    :func:`format_day_month` helper.  Falls back to the first ten
    characters of the raw string when parsing fails so the template
    still has *something* to render.

    Delegates to :func:`mediaman.core.time.parse_iso_strict_utc`, which
    preserves the previous inline ``datetime.fromisoformat`` behaviour
    exactly: any value that the old code would have routed to the
    string-slice fallback still does.
    """
    text = str(raw_added or "")
    if not text:
        return ""
    parsed = parse_iso_strict_utc(text)
    if parsed is None:
        return text[:10]
    return format_day_month(parsed, long_month=True)
