"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.session_store`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.session_store as _real
from mediaman.web.auth.session_store import (
    _EXPIRED_CLEANUP_INTERVAL,
    _FINGERPRINT_BUCKETS,
    _FINGERPRINT_MODE_ENV,
    _HARD_EXPIRY_DAYS,
    _IDLE_TIMEOUT_HOURS,
    _SESSION_REFRESH_MIN_INTERVAL,
    _SESSION_TOKEN_RE,
    _VALID_FINGERPRINT_MODES,
    SessionMetadata,
    _cleanup_expired_with_commit,
    _cleanup_lock,
    _client_fingerprint,
    _delete_session_with_commit,
    _exec_with_commit,
    _fingerprint_mode,
    _hash_token,
    _last_cleanup_at,
    _parse_last_used,
    _refresh_last_used_with_commit,
    _try_delete_session,
    create_session,
    destroy_all_sessions_for,
    destroy_session,
    list_sessions_for,
    validate_session,
)

sys.modules[__name__] = _real
