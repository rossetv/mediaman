"""Recent-reauth ticket flow.

Routes
------
- POST /api/auth/reauth — establish a recent-reauth ticket for the current session
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.auth.reauth import (
    grant_recent_reauth,
    reauth_window_seconds,
    verify_reauth_password,
)
from mediaman.web.middleware.rate_limit import rate_limit
from mediaman.web.models.users import ReauthBody
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.users.rate_limits import _REAUTH_LIMITER

logger = logging.getLogger("mediaman")

router = APIRouter()


@router.post("/api/auth/reauth")
@rate_limit(_REAUTH_LIMITER, key="actor")
def api_reauth(
    request: Request,
    body: ReauthBody,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """Establish a "recent reauth" ticket for the current session.

    Required prelude to any privilege-establishing endpoint. The ticket
    is bound to the SHA-256 hash of the caller's session token so:

    * Logout / session rotation cascades the ticket away.
    * A different session belonging to the same user does not benefit
      from this reauth.

    Wrong-password attempts feed the ``reauth:<admin>`` lockout namespace
    so a stolen session cookie cannot turn this endpoint into an
    offline-style password oracle.
    """
    conn = get_db()
    if not session_token:
        # Belt-and-braces: get_current_admin would already have raised if
        # the token was missing, but guard anyway so we never persist a
        # ticket against an empty key.
        return respond_err("not_authenticated", status=401)

    if not verify_reauth_password(conn, admin, body.password):
        logger.warning("reauth.failed actor=%s ip=%s", admin, get_client_ip(request))
        security_event(
            conn,
            event="reauth.failed",
            actor=admin,
            ip=get_client_ip(request),
        )
        return respond_err("wrong_password", status=403, message="Password is incorrect")

    grant_recent_reauth(conn, session_token, admin)
    logger.info("reauth.granted actor=%s ip=%s", admin, get_client_ip(request))
    security_event(
        conn,
        event="reauth.granted",
        actor=admin,
        ip=get_client_ip(request),
        detail={"window_seconds": reauth_window_seconds()},
    )
    return respond_ok({"expires_in_seconds": reauth_window_seconds()})
