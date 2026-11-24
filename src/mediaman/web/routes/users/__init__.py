"""User-management routes package, assembled from .crud (list, create, delete, unlock),
.sessions, .passwords, .reauth and .rate_limits (all ActionRateLimiter instances).

The combined router is exported here, and the rate limiters are re-exported so tests can
keep resetting them via mediaman.web.routes.users.
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
from mediaman.web.routes.users.rate_limits import (
    _SESSIONS_REVOKE_LIMITER as _SESSIONS_REVOKE_LIMITER,
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
    "_SESSIONS_REVOKE_LIMITER",
    "_USER_CREATE_LIMITER",
    "_USER_MGMT_LIMITER",
    "router",
]
