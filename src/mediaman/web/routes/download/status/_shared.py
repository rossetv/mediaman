"""Leaf helpers shared by the Radarr and Sonarr status projections; none touch the network or
the database.
"""

from __future__ import annotations

from mediaman.core.format import minutes_left, time_left


def _timeleft_seconds(timeleft: object) -> int | None:
    """Seconds in Radarr's or Sonarr's "[d.]hh:mm:ss"; None for anything else."""
    if not isinstance(timeleft, str) or not timeleft:
        return None
    days, _, clock = (
        timeleft.rpartition(".") if "." in timeleft.split(":")[0] else ("", "", timeleft)
    )
    parts = clock.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, mins, secs = (int(p) for p in parts)
        return int(days or 0) * 86400 + hours * 3600 + mins * 60 + secs
    except ValueError:
        return None


def _timeleft_minutes(timeleft: object) -> int | None:
    seconds = _timeleft_seconds(timeleft)
    return None if seconds is None else minutes_left(seconds)


def _format_timeleft(timeleft: str) -> str:
    minutes = _timeleft_minutes(timeleft)
    return "" if minutes is None else time_left(minutes)


def _eta(state: str, timeleft: str) -> tuple[str, int | None]:
    """The time left of a download in *state*, in words and in minutes: none while it waits its
    turn."""
    if state == "almost_ready":
        return "Post-processing…", None
    minutes = None if state == "queued" else _timeleft_minutes(timeleft)
    return ("" if minutes is None else time_left(minutes)), minutes


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
