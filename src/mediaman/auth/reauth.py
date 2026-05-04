"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.reauth`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.reauth as _real
from mediaman.web.auth.reauth import (
    _DEFAULT_REAUTH_WINDOW_SECONDS,
    _REAUTH_WINDOW_ENV,
    REAUTH_LOCKOUT_PREFIX,
    _ensure_table,
    _now,
    _require_reauth,
    cleanup_expired_reauth,
    grant_recent_reauth,
    has_recent_reauth,
    reauth_window_seconds,
    require_reauth,
    revoke_all_reauth_for,
    revoke_reauth,
    revoke_reauth_by_hash,
    revoke_reauth_by_hash_in_tx,
    verify_reauth_password,
)

sys.modules[__name__] = _real
