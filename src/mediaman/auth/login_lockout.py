"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.login_lockout`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.login_lockout as _real
from mediaman.web.auth.login_lockout import (
    _DECAY_HOURS,
    _LOCK_RULES,
    _ensure_table,
    _iso,
    _now,
    _window_for_count,
    admin_unlock,
    check_lockout,
    record_failure,
    record_success,
)

sys.modules[__name__] = _real
