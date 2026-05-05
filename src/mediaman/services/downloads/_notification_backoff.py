"""In-process exponential backoff for transient *arr outages.

When Radarr/Sonarr is unreachable (or returns ready=False) the original
loop claimed every pending row, found nothing ready, and released — once
per scheduler tick. With a sticky outage and N pending rows that's N×
claim/release cycles per minute for nothing. We keep an in-memory
``next_retry_at`` per row id and skip the row while its backoff is
active.  The state is process-local — a restart wipes it (which matches
the existing reconcile-on-startup story).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mediaman.core.backoff import ExponentialBackoff

_BACKOFF_BASE_SECONDS = 60.0  # first retry waits 1 minute
_BACKOFF_MAX_SECONDS = 1800.0  # cap at 30 minutes
_NOTIFY_BACKOFF = ExponentialBackoff(_BACKOFF_BASE_SECONDS, _BACKOFF_MAX_SECONDS)
_backoff_state: dict[int, tuple[int, datetime]] = {}


def _is_backed_off(row_id: int, now: datetime) -> bool:
    """Return True if *row_id* should be skipped this tick due to backoff."""
    record = _backoff_state.get(row_id)
    if record is None:
        return False
    _attempts, next_retry_at = record
    return now < next_retry_at


def _record_arr_failure(row_id: int, now: datetime) -> None:
    """Bump the backoff counter for *row_id* and schedule the next retry."""
    attempts, _next = _backoff_state.get(row_id, (0, now))
    attempts += 1
    delay = _NOTIFY_BACKOFF.delay(attempts)
    _backoff_state[row_id] = (attempts, now + timedelta(seconds=delay))


def _clear_backoff(row_id: int) -> None:
    """Forget the backoff record for *row_id* once it has cleared."""
    _backoff_state.pop(row_id, None)
