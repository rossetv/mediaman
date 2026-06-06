"""In-process backoff helpers for transient *arr outages and in-progress polls.

Two distinct failure modes are tracked separately:

1. **Arr-failure backoff** (``_record_arr_failure`` / ``_is_backed_off``):
   exponential backoff applied when Radarr/Sonarr is genuinely unreachable
   (network error, 5xx, ArrError).  Caps at 30 minutes so a sticky outage
   doesn't burn N claim/release cycles per minute.

2. **In-progress poll throttle** (``_record_poll_attempt``):
   short fixed-interval throttle (60 seconds) applied when an item is still
   downloading but the *arr service is reachable.  Uses the same in-memory
   ``next_retry_at`` table so the main ``_is_backed_off`` gate covers both,
   but the delay does NOT accumulate exponentially — a download that takes
   4 hours is re-evaluated every ~60 s, not at the 30-minute cap.

Keeping these separate prevents a movie that takes hours to download from
accruing exponential delay just because it wasn't ready yet (B1).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from mediaman.core.backoff import ExponentialBackoff

_BACKOFF_BASE_SECONDS = 60.0  # first arr-failure retry waits 1 minute
_BACKOFF_MAX_SECONDS = 1800.0  # cap at 30 minutes
# rationale: _NOTIFY_BACKOFF is a module-level singleton that owns the
# ExponentialBackoff delay schedule; a global is needed because the delay
# function is called from two places (arr-failure recording and poll-attempt
# recording) and the schedule must be shared across both to remain consistent.
_NOTIFY_BACKOFF = ExponentialBackoff(_BACKOFF_BASE_SECONDS, _BACKOFF_MAX_SECONDS)

# Short fixed delay for items that are still downloading (not an error).
_POLL_INTERVAL_SECONDS = 60.0

# rationale: Per-row backoff state mutated from the APScheduler thread pool;
# the Lock guards concurrent fires of the notification check.
_backoff_state: dict[int, tuple[int, datetime]] = {}
_backoff_state_lock = threading.Lock()


def _is_backed_off(row_id: int, now: datetime) -> bool:
    """Return True if *row_id* should be skipped this tick due to backoff or throttle."""
    with _backoff_state_lock:
        record = _backoff_state.get(row_id)
    if record is None:
        return False
    _attempts, next_retry_at = record
    return now < next_retry_at


def _record_arr_failure(row_id: int, now: datetime) -> None:
    """Bump the exponential backoff counter for *row_id* on a genuine *arr failure.

    Only call this when Radarr/Sonarr is actually unreachable or returned an
    error.  Do NOT call for items that are still downloading — use
    :func:`_record_poll_attempt` instead.
    """
    with _backoff_state_lock:
        attempts, _next = _backoff_state.get(row_id, (0, now))
        attempts += 1
        delay = _NOTIFY_BACKOFF.delay(attempts)
        _backoff_state[row_id] = (attempts, now + timedelta(seconds=delay))


def _record_poll_attempt(row_id: int, now: datetime) -> None:
    """Schedule a short fixed-interval re-evaluation for an in-progress download.

    Sets ``next_retry_at = now + _POLL_INTERVAL_SECONDS`` without incrementing
    the failure counter, so the exponential backoff schedule is unaffected.
    A long download gets re-evaluated every ~60 s rather than accruing the
    same exponential delay as a genuine *arr outage.
    """
    with _backoff_state_lock:
        existing = _backoff_state.get(row_id)
        # Preserve the existing attempt count (could be non-zero from a prior
        # arr failure that has since cleared) but override the next_retry_at
        # with the short poll interval.
        attempts = existing[0] if existing else 0
        _backoff_state[row_id] = (attempts, now + timedelta(seconds=_POLL_INTERVAL_SECONDS))


def _clear_backoff(row_id: int) -> None:
    """Forget the backoff/throttle record for *row_id* once it has cleared."""
    with _backoff_state_lock:
        _backoff_state.pop(row_id, None)
