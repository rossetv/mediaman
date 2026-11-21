"""In-process backoff for transient *arr outages and in-progress polls.

Two failure modes are tracked separately. Arr failures (unreachable, 5xx, ArrError) back
off exponentially, capped at 30 minutes. Items still downloading get a fixed ~60 s
re-check that does not accumulate, so a download taking hours is not delayed as if the
arr service were down. Both use the same in-memory next_retry_at table read by
_is_backed_off.
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
    with _backoff_state_lock:
        record = _backoff_state.get(row_id)
    if record is None:
        return False
    _attempts, next_retry_at = record
    return now < next_retry_at


def _record_arr_failure(row_id: int, now: datetime) -> None:
    """Bump the exponential backoff for *row_id*. Only for genuine arr failures; use
    _record_poll_attempt for items still downloading.
    """
    with _backoff_state_lock:
        attempts, _next = _backoff_state.get(row_id, (0, now))
        attempts += 1
        delay = _NOTIFY_BACKOFF.delay(attempts)
        _backoff_state[row_id] = (attempts, now + timedelta(seconds=delay))


def _record_poll_attempt(row_id: int, now: datetime) -> None:
    """Set next_retry_at to now + _POLL_INTERVAL_SECONDS without touching the failure
    counter.
    """
    with _backoff_state_lock:
        existing = _backoff_state.get(row_id)
        # Preserve the existing attempt count (could be non-zero from a prior
        # arr failure that has since cleared) but override the next_retry_at
        # with the short poll interval.
        attempts = existing[0] if existing else 0
        _backoff_state[row_id] = (attempts, now + timedelta(seconds=_POLL_INTERVAL_SECONDS))


def _clear_backoff(row_id: int) -> None:
    with _backoff_state_lock:
        _backoff_state.pop(row_id, None)
