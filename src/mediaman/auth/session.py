"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.session`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.session as _real
from mediaman.web.auth.session import (
    _DUMMY_HASH,
    _DUMMY_HASH_LOCK,
    _HARD_EXPIRY_DAYS,
    _IDLE_TIMEOUT_HOURS,
    _SESSION_REFRESH_MIN_INTERVAL,
    _SESSION_TOKEN_RE,
    SessionMetadata,
    _client_fingerprint,
    _fingerprint_mode,
    _get_dummy_hash,
    _hash_token,
    authenticate,
    change_password,
    create_session,
    create_user,
    delete_user,
    destroy_all_sessions_for,
    destroy_session,
    list_sessions_for,
    list_users,
    set_must_change_password,
    user_must_change_password,
    validate_session,
)

sys.modules[__name__] = _real
