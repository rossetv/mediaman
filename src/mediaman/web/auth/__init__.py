"""Web authentication package.

Relocated from :mod:`mediaman.auth` (which now contains only a back-compat
shim). This package owns all web-specific authentication concerns: sessions,
login lockout, password hashing and policy, reauth tickets, and rate limiting.

All sub-modules are importable directly from this package or from their
canonical paths under ``mediaman.web.auth.*``.
"""

from mediaman.web.auth import (
    _token_hashing,
    cli,
    login_lockout,
    middleware,
    password_hash,
    password_policy,
    rate_limit,
    reauth,
    session,
    session_store,
)

__all__ = [
    "_token_hashing",
    "cli",
    "login_lockout",
    "middleware",
    "password_hash",
    "password_policy",
    "rate_limit",
    "reauth",
    "session",
    "session_store",
]
