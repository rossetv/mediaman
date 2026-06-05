"""Module-level in-memory throttle state, lock, backoff constants, and helpers.

All mutable per-item and per-arr-instance state lives here so it can be
imported by both :mod:`mediaman.services.arr.search_trigger` (decision
logic) and :mod:`mediaman.services.arr._throttle_persistence` (DB layer)
without introducing a circular dependency.

State summary:

* ``_last_search_trigger`` — dl_id → epoch of last successful trigger.
* ``_search_count`` — dl_id → number of fires since process start.
* ``_reservation_tokens`` — dl_id → UUID hex identifying the current
  in-flight reservation attempt.
* ``_last_search_trigger_by_arr`` — ``service`` (``"radarr"``/``"sonarr"``)
  → ``(epoch, dl_id)`` of the last trigger for that whole arr instance. This
  is the per-arr-instance fan-out cap: a search is skipped if *another* item
  on the same instance fired within :data:`_PER_ARR_THROTTLE_SECONDS`. The
  stored ``dl_id`` lets the cap distinguish two cases that must behave
  differently: a *different* dl_id within the window is blocked (caps fan-out
  across distinct items, and closes the title-rename bypass — a renamed
  series presents a fresh dl_id); the *same* dl_id is allowed through so an
  item can still advance along its own per-item backoff curve.
* ``_state_lock`` — threading.Lock guarding all four dicts above.

All names in this module are re-exported verbatim from
:mod:`mediaman.services.arr.search_trigger` so existing import paths
and test monkeypatch targets continue to work.
"""

from __future__ import annotations

import threading

from mediaman.core.backoff import ExponentialBackoff

# ---- Persisted/in-memory throttle state ----

# Maps dl_id -> epoch seconds of last trigger.
_last_search_trigger: dict[str, float] = {}

# Parallel map: dl_id -> number of times we've triggered a search for this
# item since process start. Powers the "Searched N times" UI hint so users
# can see mediaman is actually poking Radarr/Sonarr rather than idling.
_search_count: dict[str, int] = {}

# Tokens identifying the current owner of each dl_id's reservation. The
# token is generated under the lock when a worker reserves the slot and
# checked again on rollback so a sibling worker overwriting the slot in
# the meantime cannot have its work undone — the token comparison
# distinguishes our own rollback from a sibling's fresh reservation.
_reservation_tokens: dict[str, str] = {}

# Per-arr-instance fan-out cap: service ("radarr"/"sonarr") -> (epoch, dl_id)
# of the last trigger on that instance. ``maybe_trigger_search`` reads this in
# phase 1 and skips when a *different* dl_id fired within
# _PER_ARR_THROTTLE_SECONDS, so 50 distinct stuck items on one instance produce
# ONE search per window, not 50 — capping indexer fan-out and closing the
# title-rename bypass (a renamed item presents a fresh dl_id). The stored dl_id
# is what lets the *same* item keep advancing along its own per-item backoff.
#
# §1.12: this is best-effort, in-process, single-worker state — it is NOT
# persisted to SQLite and resets to empty on restart (the cap simply re-warms
# over the next window). The dict is naturally tiny — one entry per configured
# arr instance, not per library item — and is popped by ``clear_throttle``, so
# it never grows with library size. Deliberately no persistence machinery: the
# fan-out cap is a soft rate-limit, not a correctness invariant.
_last_search_trigger_by_arr: dict[str, tuple[float, str]] = {}

# Lock guarding _last_search_trigger, _search_count, _reservation_tokens,
# and _last_search_trigger_by_arr.
_state_lock = threading.Lock()


# ---- Backoff configuration ----

# Per-item exponential backoff. interval(n) = base * 2^max(n-1, 0), clamped.
# n is the number of fires already completed for this dl_id; the gate uses it
# to compute the wait until the next allowed fire.
_SEARCH_BACKOFF_BASE_SECONDS = 120  # 2 min
_SEARCH_BACKOFF_MAX_SECONDS = 86_400  # 24 h cap
_SEARCH_BACKOFF_JITTER = 0.1  # ±10% multiplicative jitter

_SEARCH_BACKOFF = ExponentialBackoff(
    _SEARCH_BACKOFF_BASE_SECONDS,
    _SEARCH_BACKOFF_MAX_SECONDS,
    jitter=_SEARCH_BACKOFF_JITTER,
)


# ---- Internal helpers ----


def _search_backoff_seconds(search_count: int, dl_id: str, last_triggered_at: float) -> float:
    """Return the wait in seconds before the next fire is allowed.

    *search_count* is the number of fires already completed for *dl_id*.
    The result is the jittered interval that gates the next fire after
    *last_triggered_at*.

    The seed encodes ``(dl_id, last_triggered_at)`` — the ``!r`` formatting
    of the float is deliberate and part of the determinism contract: it
    produces a consistent representation across platforms and Python versions,
    unlike bare ``str(float)``.  See :class:`~mediaman.core.backoff.ExponentialBackoff`
    for why determinism is load-bearing here.

    Tests neutralise jitter by monkeypatching
    ``_SEARCH_BACKOFF.deterministic_multiplier`` to return a constant.
    """
    seed = f"{dl_id}|{last_triggered_at!r}".encode()
    return _SEARCH_BACKOFF.delay(search_count, seed=seed)


def _arr_throttle_key(service: str) -> str:
    """Return the per-arr-instance fan-out-cap key.

    Keyed by *service* alone ("radarr"/"sonarr") because a mediaman deploy
    has exactly one configured instance per service, and the cap is meant to
    bound fan-out across the *whole* instance — not per item. Independent of
    ``arr_id``/``dl_id``, so a title rename can't mint a fresh key and dodge
    the cap.
    """
    return service
