from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from mediaman.core.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.reauth import revoke_all_reauth_for, revoke_reauth
from mediaman.web.auth.session_store import (
    create_session,
    destroy_all_sessions_for,
    list_sessions_for,
)
from mediaman.web.cookies import is_request_secure, set_session_cookie
from mediaman.web.middleware.rate_limit import rate_limit
from mediaman.web.responses import respond_ok
from mediaman.web.routes.users.rate_limits import _SESSIONS_LIST_LIMITER

logger = logging.getLogger(__name__)

router = APIRouter()

#: Keys returned to the client by api_list_sessions.
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
def api_list_sessions(request: Request, admin: str = Depends(get_current_user)) -> Response:
    """List active sessions for the signed-in user.

    Returns timestamp metadata only, never raw tokens, IPs or fingerprints: a cookie thief
    could use a fingerprint to detect the legitimate user logging in or to forge a match on
    a third device. Rate limited per actor (_SESSIONS_LIST_LIMITER) for the same reason.
    """
    conn = get_db()
    safe = [
        {key: row.get(key) for key in _SESSION_SAFE_KEYS} for row in list_sessions_for(conn, admin)
    ]
    return JSONResponse({"sessions": safe})


@router.post("/api/users/sessions/revoke-others")
def api_revoke_other_sessions(
    request: Request,
    admin: str = Depends(get_current_user),
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """Revoke every session for the signed-in user except the current one. Reauth tickets bound
    to the destroyed sessions are dropped too so they cannot be reused.
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

    response = respond_ok(
        {
            "revoked": destroyed,
            "message": "Other sessions revoked. You are now logged in with a fresh token.",
        }
    )
    set_session_cookie(response, new_token, secure=is_request_secure(request))
    logger.info("session.revoke_all user=%s revoked=%d", _sanitise_log_field(admin), destroyed)
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
