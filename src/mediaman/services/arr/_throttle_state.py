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
* ``_last_search_trigger_by_arr`` — ``"{service}:#{arr_id}"`` → epoch of
  last trigger, indexed by the arr-id-stable composite key so a title
  rename cannot bypass the throttle by producing a fresh dl_id.
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

# Stable composite-key throttle indexed by ``f"{service}:#{arr_id}"``.
# The ``dl_id``-based throttle in ``_last_search_trigger`` collapses
# under a Sonarr/Radarr title rename (the title-derived dl_id changes),
# but ``arr_id`` is stable. ``maybe_trigger_search`` mirrors every
# successful trigger here so a renamed series can't bypass the throttle
# by producing a fresh title-derived dl_id.
_last_search_trigger_by_arr: dict[str, float] = {}

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


def _jitter_for(dl_id: str, last_triggered_at: float) -> float:
    """Return the deterministic ±10% jitter multiplier for *(dl_id, last_triggered_at)*.

    Kept as a thin shim so existing tests can ``monkeypatch`` it to
    pin the multiplier to a constant when asserting on the unjittered
    backoff curve.  Production code routes through ``_SEARCH_BACKOFF.delay``,
    which calls into :class:`~mediaman.services.infra.backoff.ExponentialBackoff`'s
    deterministic-multiplier helper using the same seed.

    Tests patch ``mediaman.services.arr._throttle_state._jitter_for``.
    ``_search_backoff_seconds`` resolves ``_jitter_for`` from this
    module's globals at call time, so the monkeypatch on this name is
    picked up by the production backoff computation. (Patching the
    re-export at ``mediaman.services.arr.search_trigger._jitter_for``
    has no effect on production behaviour, since ``search_trigger``
    does not call ``_jitter_for`` directly.)
    """
    seed = f"{dl_id}|{last_triggered_at!r}".encode()
    return _SEARCH_BACKOFF._deterministic_multiplier(seed)


def _search_backoff_seconds(search_count: int, dl_id: str, last_triggered_at: float) -> float:
    """Return the wait in seconds before the next fire is allowed.

    *search_count* is the number of fires already completed for *dl_id*.
    The result is the jittered interval that gates the next fire after
    *last_triggered_at*.

    The seed encodes ``(dl_id, last_triggered_at)`` — the ``!r`` formatting
    of the float is deliberate and part of the determinism contract: it
    produces a consistent representation across platforms and Python versions,
    unlike bare ``str(float)``.  See :class:`~mediaman.services.infra.backoff.ExponentialBackoff`
    for why determinism is load-bearing here.

    Routes the multiplier through the module-level ``_jitter_for`` name so
    that test monkeypatches on
    ``mediaman.services.arr._throttle_state._jitter_for`` override the
    multiplier as expected.
    """
    n = max(search_count, 0)
    base = min(_SEARCH_BACKOFF_BASE_SECONDS * 2 ** max(n - 1, 0), _SEARCH_BACKOFF_MAX_SECONDS)
    return min(base * _jitter_for(dl_id, last_triggered_at), _SEARCH_BACKOFF_MAX_SECONDS)


def _arr_throttle_key(service: str, arr_id: int) -> str:
    """Return the stable arr-id-based throttle key.

    Used as a parallel index to ``_last_search_trigger`` (which is keyed
    by ``dl_id``). The ``dl_id`` collapses under a title rename;
    the arr-id key does not. ``maybe_trigger_search`` updates both, so
    any path that has access to ``(service, arr_id)`` can dedupe even
    if the title has changed since the last trigger.
    """
    return f"{service}:#{arr_id}"
