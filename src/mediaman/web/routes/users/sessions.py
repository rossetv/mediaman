"""Session-management routes.

Routes
------
- GET  /api/users/sessions                  — list own active sessions
- POST /api/users/sessions/revoke-others    — revoke all other sessions and re-issue cookie
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from mediaman.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.auth.reauth import revoke_all_reauth_for, revoke_reauth
from mediaman.web.auth.session_store import (
    create_session,
    destroy_all_sessions_for,
    list_sessions_for,
)
from mediaman.web.middleware.rate_limit import rate_limit
from mediaman.web.responses import respond_ok
from mediaman.web.routes._helpers import set_session_cookie
from mediaman.web.routes.users.rate_limits import _SESSIONS_LIST_LIMITER

logger = logging.getLogger(__name__)

router = APIRouter()

#: Keys returned to the client by :func:`api_list_sessions`.
#:
#: ``issued_ip`` and ``fingerprint`` are deliberately stripped: the
#: fingerprint exposes the SHA-256-prefix of the user-agent + the /24 of
#: the issuing IP, which is exactly the material an attacker holding a
#: stolen cookie would need to forge a matching fingerprint header on
#: another device. ``issued_ip`` is the legitimate user's IP and useful
#: for nothing but reconnaissance / doxxing. Timestamps are enough for
#: the UI to render "this is your current session" without leaking either.
_SESSION_SAFE_KEYS = ("created_at", "expires_at", "last_used_at")


@router.get("/api/users/sessions")
@rate_limit(_SESSIONS_LIST_LIMITER, key="actor")
def api_list_sessions(request: Request, admin: str = Depends(get_current_admin)) -> Response:
    """List active sessions for the current admin.

    Returns timestamp metadata only — never raw tokens, IPs, or
    fingerprints (a stolen cookie holder could otherwise use the
    fingerprint to detect the legitimate user logging in or to forge a
    matching fingerprint on a third device). Use
    ``/api/users/sessions/revoke-others`` to log out other devices.

    Per-actor rate limited (:data:`_SESSIONS_LIST_LIMITER`) so a cookie
    thief cannot poll this to detect the moment the legitimate user
    signs in.
    """
    conn = get_db()
    safe = [
        {key: row.get(key) for key in _SESSION_SAFE_KEYS} for row in list_sessions_for(conn, admin)
    ]
    return JSONResponse({"sessions": safe})


@router.post("/api/users/sessions/revoke-others")
def api_revoke_other_sessions(
    request: Request,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """Revoke every session for the current admin EXCEPT the current one.

    Useful after "I think my cookie leaked" — the admin keeps working on
    their current tab while every other device is logged out. We also drop
    any reauth tickets bound to the destroyed sessions so they cannot be
    reused.
    """
    conn = get_db()
    # Drop reauth tickets for this user before destroying their sessions
    # so a thief who held a ticket cannot perform one last privileged
    # action between the destroy and the new-session create.
    revoke_all_reauth_for(conn, admin)
    destroyed = destroy_all_sessions_for(conn, admin)
    client_ip = get_client_ip(request)

    new_token = create_session(
        conn,
        admin,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=client_ip,
    )

    from mediaman.web.routes.auth import is_request_secure

    response = respond_ok(
        {
            "revoked": destroyed,
            "message": "Other sessions revoked. You are now logged in with a fresh token.",
        }
    )
    set_session_cookie(response, new_token, secure=is_request_secure(request))
    logger.info("session.revoke_all user=%s revoked=%d", admin, destroyed)
    security_event(
        conn,
        event="session.revoke_all",
        actor=admin,
        ip=client_ip,
        detail={"revoked": destroyed},
    )
    # Belt-and-braces: drop any ticket that may have been re-granted for
    # the freshly issued token (none should exist, but it costs us nothing
    # to ensure the old ticket's data is gone).
    if session_token:
        revoke_reauth(conn, session_token)
    return response
