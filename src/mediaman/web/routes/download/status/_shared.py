"""Numeric coercion and formatting helpers shared by the status projections.

These are the leaf helpers used by both the Radarr (:mod:`._radarr`) and
Sonarr (:mod:`._sonarr`) status-projection modules — none of them touch the
network or the database, so they live apart from the projection logic that
does. ``_format_timeleft`` is additionally re-exported from the package
barrel because the unit tests exercise it directly.
"""

from __future__ import annotations


def _format_timeleft(timeleft: str) -> str:
    """Convert HH:MM:SS timeleft string to a human-readable eta string."""
    if not timeleft:
        return ""
    parts = timeleft.split(":")
    if len(parts) != 3:
        return ""
    try:
        hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return ""
    if hours > 0:
        return f"~{hours} hr {mins:02d} min remaining"
    if mins > 0:
        return f"~{mins} min remaining"
    return f"~{max(1, secs)} sec remaining"


def _safe_int(value: object) -> int:
    """Coerce *value* to a non-negative int, defaulting to 0.

    Defends against Arr responses that return ``size`` / ``sizeleft`` as
    strings or null. Previously ``size_total > 0`` raised ``TypeError``
    on a string operand and crashed the handler.

    Accepts ``int``/``float`` directly and parses ``str`` numerals;
    everything else (including ``None`` and ``bool``) resolves to 0.
    """
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` so ``int(True)`` is valid,
        # but treating ``True`` as a 1-byte size makes no sense.
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        try:
            n = int(value)
        except ValueError:
            return 0
        return n if n > 0 else 0
    return 0


def _safe_progress(size_total: int, size_left: int) -> int:
    """Return a download progress percentage clamped to ``[0, 100]``.

    Without the clamp a misreported ``sizeleft`` larger than ``size``
    (or a negative ``sizeleft``) would yield an out-of-range progress
    value that breaks the progress-bar template — a UI hazard rather
    than a data corruption issue, but still worth defending against.
    """
    if size_total <= 0:
        return 0
    raw = round((1 - size_left / size_total) * 100)
    return max(0, min(100, raw))
