from __future__ import annotations

import logging
import sqlite3

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
    end_other_session,
    list_sessions_for,
)
from mediaman.web.cookies import is_request_secure, set_session_cookie
from mediaman.web.middleware.rate_limit import rate_limit
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.plex_link import carry_attempt
from mediaman.web.routes.users.crud import _reserve
from mediaman.web.routes.users.rate_limits import (
    _SESSIONS_LIST_LIMITER,
    _SESSIONS_REVOKE_LIMITER,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: Keys returned to the client by api_list_sessions.
#:
#: ``issued_ip`` and ``fingerprint`` are deliberately stripped: the
#: fingerprint exposes the SHA-256-prefix of the user-agent + the /24 of
#: the issuing IP, which is exactly the material an attacker holding a
#: stolen cookie would need to forge a matching fingerprint header on
#: another device. ``issued_ip`` is the legitimate user's IP and useful
#: for nothing but reconnaissance / doxxing. Timestamps, the device's name and whether the
#: session is the caller's own are enough to tell one from another, and ``public_id``, random and
#: never the token or its hash, to end one.
_SESSION_SAFE_KEYS = (
    "created_at",
    "expires_at",
    "last_used_at",
    "current",
    "device_label",
    "public_id",
)


@router.get("/api/users/sessions")
@rate_limit(_SESSIONS_LIST_LIMITER, key="actor")
def api_list_sessions(
    request: Request,
    admin: str = Depends(get_current_user),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """List active sessions for the signed-in user.

    Returns timestamps, device names, public ids and which session is the caller's, never raw tokens, IPs or fingerprints: a cookie thief
    could use a fingerprint to detect the legitimate user logging in or to forge a match on
    a third device. Rate limited per actor (_SESSIONS_LIST_LIMITER) for the same reason.
    """
    conn = get_db()
    safe = [
        {key: row.get(key) for key in _SESSION_SAFE_KEYS}
        for row in list_sessions_for(conn, admin, current_token=session_token)
    ]
    return JSONResponse({"sessions": safe})


@router.post("/api/users/sessions/revoke-others")
def api_revoke_other_sessions(
    request: Request,
    admin: str = Depends(get_current_user),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Revoke every session for the signed-in user except the current one. Requires a recent
    reauth ticket, so a stolen session cookie cannot sign the owner out. Reauth tickets bound
    to the destroyed sessions are dropped too so they cannot be reused.
    """
    reserved = _reserve(
        request,
        _SESSIONS_REVOKE_LIMITER,
        admin,
        session_token,
        "session.revoke_all",
        what="Too many tries.",
    )
    if isinstance(reserved, Response):
        return reserved
    conn = get_db()
    # Drop reauth tickets for this user before destroying their sessions
    # so a thief who held a ticket cannot perform one last privileged
    # action between the destroy and the new-session create.
    revoke_all_reauth_for(conn, admin)
    # The caller's own session is destroyed too, but replaced below.
    ended = max(destroy_all_sessions_for(conn, admin) - 1, 0)
    client_ip = get_client_ip(request)

    new_token = create_session(
        conn,
        admin,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=client_ip,
    )
    carry_attempt(session_token, new_token)

    response = respond_ok(
        {
            "revoked": ended,
            "message": "Other sessions revoked. You are now logged in with a fresh token.",
        }
    )
    set_session_cookie(response, new_token, secure=is_request_secure(request))
    logger.info("session.revoke_all user=%s revoked=%d", _sanitise_log_field(admin), ended)
    security_event(
        conn,
        event="session.revoke_all",
        actor=admin,
        ip=client_ip,
        detail={"revoked": ended},
    )
    # Belt-and-braces: drop any ticket that may have been re-granted for
    # the freshly issued token (none should exist, but it costs us nothing
    # to ensure the old ticket's data is gone).
    if session_token:
        revoke_reauth(conn, session_token)
    return response


@router.delete("/api/users/sessions/{public_id}")
def api_revoke_session(
    public_id: str,
    request: Request,
    admin: str = Depends(get_current_user),
    session_token: str = Cookie(default=""),
) -> Response:
    """End one other session of the signed-in user. Requires a recent reauth ticket, so a stolen
    session cookie cannot sign the owner's devices out one by one. A session already gone and
    another person's answer alike, as done.
    """
    reserved = _reserve(
        request,
        _SESSIONS_REVOKE_LIMITER,
        admin,
        session_token,
        "session.revoke_one",
        what="Too many tries.",
    )
    if isinstance(reserved, Response):
        return reserved
    try:
        outcome = end_other_session(
            get_db(), admin, public_id, current_token=session_token, ip=get_client_ip(request)
        )
    except sqlite3.Error:
        logger.exception("session.revoke_one failed user=%s", _sanitise_log_field(admin))
        return respond_err(
            "internal_error", status=500, message="Could not sign that device out. Try again."
        )
    if outcome == "current":
        _SESSIONS_REVOKE_LIMITER.release(admin, reserved)
        return respond_err(
            "current_session", status=400, message="Use Sign out to end this session."
        )
    return respond_ok()
