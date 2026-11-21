"""Leaf helpers shared by the Radarr and Sonarr status projections; none touch the network or
the database.
"""

from __future__ import annotations


def _format_timeleft(timeleft: str) -> str:
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
    """Coerce value to a non-negative int, defaulting to 0. Arr responses may carry size fields
    as strings or null; ints, floats and numeric strings are accepted, everything else
    (including None and bool) is 0.
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
    """Return download progress clamped to [0, 100]; a misreported sizeleft would otherwise
    break the progress-bar template.
    """
    if size_total <= 0:
        return 0
    raw = round((1 - size_left / size_total) * 100)
    return max(0, min(100, raw))
