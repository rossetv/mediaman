"""Back-compat shim — auth package relocated to :mod:`mediaman.web.auth`.

New code should import from ``mediaman.web.auth`` directly. Existing
imports through ``mediaman.auth.*`` continue to work via the module-level
shims in each sibling file (e.g. ``mediaman.auth.session_store``,
``mediaman.auth.middleware``, etc.).

Sub-modules are re-imported here so that
``from mediaman.auth import session_store`` style imports resolve:
"""

# ruff: noqa: F401 — deliberate re-export shim.

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
