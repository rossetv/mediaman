"""User-management routes package.

This package splits the original monolithic ``users.py`` module into focused
sub-modules:

- :mod:`.crud`       — list / create / delete / unlock
- :mod:`.sessions`   — list sessions, revoke-others
- :mod:`.passwords`  — change password
- :mod:`.reauth`     — reauth ticket flow
- :mod:`.rate_limits` — all module-level :class:`~mediaman.auth.rate_limit.ActionRateLimiter`
                         instances

The combined ``router`` is assembled here and exported so that the existing
``from mediaman.web.routes.users import router`` import continues to work for
callers and tests.

Rate-limiter instances are also re-exported from this namespace so that
``from mediaman.web.routes.users import _USER_MGMT_LIMITER`` (used by tests
to reset limiters between cases) continues to work without modification.
"""

from __future__ import annotations

from fastapi import APIRouter

from mediaman.web.routes.users.crud import router as _crud_router
from mediaman.web.routes.users.passwords import router as _passwords_router
from mediaman.web.routes.users.rate_limits import (
    _PASSWORD_CHANGE_IP_LIMITER as _PASSWORD_CHANGE_IP_LIMITER,
)
from mediaman.web.routes.users.rate_limits import (
    _PASSWORD_CHANGE_LIMITER as _PASSWORD_CHANGE_LIMITER,
)
from mediaman.web.routes.users.rate_limits import _REAUTH_LIMITER as _REAUTH_LIMITER
from mediaman.web.routes.users.rate_limits import (
    _SESSIONS_LIST_LIMITER as _SESSIONS_LIST_LIMITER,
)
from mediaman.web.routes.users.rate_limits import _USER_CREATE_LIMITER as _USER_CREATE_LIMITER
from mediaman.web.routes.users.rate_limits import _USER_MGMT_LIMITER as _USER_MGMT_LIMITER
from mediaman.web.routes.users.reauth import router as _reauth_router
from mediaman.web.routes.users.sessions import router as _sessions_router

router = APIRouter()
router.include_router(_crud_router)
router.include_router(_sessions_router)
router.include_router(_passwords_router)
router.include_router(_reauth_router)

__all__ = [
    "_PASSWORD_CHANGE_IP_LIMITER",
    "_PASSWORD_CHANGE_LIMITER",
    "_REAUTH_LIMITER",
    "_SESSIONS_LIST_LIMITER",
    "_USER_CREATE_LIMITER",
    "_USER_MGMT_LIMITER",
    "router",
]
