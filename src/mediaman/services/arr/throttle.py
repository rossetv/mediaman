"""Back-compat shim — all content has moved to :mod:`mediaman.services.arr.search_trigger`.

Everything that used to live here (throttle state, backoff configuration,
persistence helpers, and inspection utilities) was merged into
:mod:`mediaman.services.arr.search_trigger` to eliminate the re-export dance
that was required to keep callers and tests happy after the original split.

This module is preserved as a re-export shim so that:

* Code that does ``from mediaman.services.arr.throttle import X`` continues
  to work unchanged.
* Tests that monkeypatch ``mediaman.services.arr.throttle._jitter_for`` (or
  any other private name) still reach a live attribute on this module.
  ``_search_backoff_seconds`` resolves ``_jitter_for`` through this module at
  call time (lazy import), so patching ``throttle._jitter_for`` propagates
  into the backoff computation.

Public names are re-exported by the ``*`` import below.  Private names
(those beginning with ``_``) must be listed explicitly because ``import *``
skips them by default.
"""

from mediaman.services.arr.search_trigger import *  # noqa: F403
from mediaman.services.arr.search_trigger import (  # noqa: F401
    _PER_ARR_THROTTLE_SECONDS,
    _SEARCH_BACKOFF,
    _SEARCH_BACKOFF_BASE_SECONDS,
    _SEARCH_BACKOFF_JITTER,
    _SEARCH_BACKOFF_MAX_SECONDS,
    _SEARCH_STALE_SECONDS,
    _STRANDED_THROTTLE_TTL_SECONDS,
    _arr_throttle_key,
    _jitter_for,
    _last_search_trigger,
    _last_search_trigger_by_arr,
    _load_throttle_from_db,
    _reservation_tokens,
    _save_trigger_to_db,
    _search_backoff_seconds,
    _search_count,
    _state_lock,
)
