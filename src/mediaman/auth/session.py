"""Admin user and session management — facade module.

Split in R2 into two sibling modules:

- :mod:`mediaman.auth.password_hash` — bcrypt hashing, verification, rotation.
- :mod:`mediaman.auth.session_store` — session persistence, validation,
  fingerprint binding, hardening.

This module re-exports every public symbol both modules define so
existing callers (web routes, CLI, tests) continue to import from
``mediaman.auth.session``.
"""
# ruff: noqa: I001, F401 — deliberate re-export facade.

from __future__ import annotations

from mediaman.auth.password_hash import (
    _DUMMY_HASH,
    _DUMMY_HASH_LOCK,
    _get_dummy_hash,
    authenticate,
    change_password,
    create_user,
    delete_user,
    list_users,
    set_must_change_password,
    user_must_change_password,
)
from mediaman.auth.session_store import (
    _HARD_EXPIRY_DAYS,
    _IDLE_TIMEOUT_HOURS,
    _SESSION_REFRESH_MIN_INTERVAL,
    _SESSION_TOKEN_RE,
    _client_fingerprint,
    _fingerprint_mode,
    _hash_token,
    SessionMetadata,
    create_session,
    destroy_all_sessions_for,
    destroy_session,
    list_sessions_for,
    validate_session,
)

__all__ = [
    "SessionMetadata",
    "authenticate",
    "change_password",
    "create_session",
    "create_user",
    "delete_user",
    "destroy_all_sessions_for",
    "destroy_session",
    "list_sessions_for",
    "list_users",
    "set_must_change_password",
    "user_must_change_password",
    "validate_session",
]
