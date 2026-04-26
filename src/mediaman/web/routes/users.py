"""User management API routes.

Handles listing, creating, deleting users, changing passwords, and managing
sessions for admin accounts.

Routes:
- GET  /api/users                      — list users
- POST /api/users                      — create user
- DELETE /api/users/{user_id}          — delete user (requires reauth)
- POST /api/users/change-password      — change own password
- GET  /api/users/sessions             — list own sessions
- POST /api/users/sessions/revoke-others — revoke other sessions
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

from mediaman.audit import security_event
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.auth.reauth import _require_reauth
from mediaman.auth.session import (
    change_password,
    create_user,
    delete_user,
    destroy_all_sessions_for,
    list_sessions_for,
    list_users,
)
from mediaman.db import get_db
from mediaman.web.routes._helpers import set_session_cookie

logger = logging.getLogger("mediaman")

router = APIRouter()

_USER_MGMT_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)
# Separate tighter limiter for user creation — an attacker who compromises
# an admin session should not be able to mass-create accounts before being
# spotted. 3 per hour / 5 per day is enough for any legitimate operator
# workflow without letting automation run unchecked.
_USER_CREATE_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=3600, max_per_day=5)


class _CreateUserBody(BaseModel):
    """Body shape for POST /api/users."""

    username: str = ""
    password: str = ""


class _ChangePasswordBody(BaseModel):
    """Body shape for POST /api/users/change-password."""

    old_password: str = ""
    new_password: str = ""


@router.get("/api/users")
def api_list_users(admin: str = Depends(get_current_admin)) -> dict[str, object]:
    """List all admin users."""
    conn = get_db()
    return {"users": list_users(conn), "current": admin}


@router.post("/api/users")
def api_create_user(
    request: Request,
    body: _CreateUserBody,
    admin: str = Depends(get_current_admin),
) -> Response:
    """Create a new admin user."""
    if not _USER_CREATE_LIMITER.check(admin):
        logger.warning("user.create_throttled actor=%s", admin)
        return JSONResponse(
            {"ok": False, "error": "Too many user-creation attempts — slow down"},
            status_code=429,
        )

    username = body.username.strip()
    password = body.password

    if not username or len(username) < 3 or len(username) > 64:
        return JSONResponse(
            {"ok": False, "error": "Username must be between 3 and 64 characters"},
            status_code=400,
        )

    from mediaman.auth.password_policy import password_issues

    issues = password_issues(password, username=username)
    if issues:
        return JSONResponse(
            {
                "ok": False,
                "error": "Password does not meet the strength policy",
                "issues": issues,
            },
            status_code=400,
        )

    conn = get_db()
    try:
        create_user(conn, username, password)
        logger.info("user.created actor=%s new_user=%s", admin, username)
        security_event(
            conn,
            event="user.created",
            actor=admin,
            ip=get_client_ip(request),
            detail={"new_username": username},
        )
        return {"ok": True, "username": username}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)


@router.delete("/api/users/{user_id}")
def api_delete_user(
    user_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
    x_confirm_password: str | None = Header(default=None),
) -> Response:
    """Delete an admin user. Cannot delete yourself.

    Requires the caller's password via the ``X-Confirm-Password`` request
    header. Passing the password in the query string is explicitly rejected
    — query strings appear in access logs, proxies, and browser history,
    making them unsuitable for credentials.

    A compromised session cookie alone cannot delete other admins.
    """
    # Reject any attempt to pass the password via query string — it leaks
    # into access logs and server-side request recording.
    if "confirm_password" in request.query_params:
        return JSONResponse(
            {
                "ok": False,
                "error": "confirm_password must be sent via X-Confirm-Password header, not the query string",
            },
            status_code=400,
        )

    conn = get_db()
    if not _USER_MGMT_LIMITER.check(admin):
        return JSONResponse(
            {"ok": False, "error": "Too many user-management operations"},
            status_code=429,
        )
    pw = x_confirm_password or ""
    if not _require_reauth(conn, admin, pw):
        logger.warning("user.delete_rejected user=%s reason=reauth_required", admin)
        return JSONResponse(
            {"ok": False, "error": "Password confirmation required"},
            status_code=403,
        )
    if delete_user(conn, user_id, admin):
        logger.info("user.deleted actor=%s target_id=%d", admin, user_id)
        security_event(
            conn,
            event="user.deleted",
            actor=admin,
            ip=get_client_ip(request),
            detail={"target_id": user_id},
        )
        return {"ok": True}
    return JSONResponse(
        {"ok": False, "error": "Cannot delete yourself or user not found"}, status_code=400
    )


@router.post("/api/users/change-password")
def api_change_password(
    request: Request,
    body: _ChangePasswordBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Change the current user's password."""
    old_password = body.old_password
    new_password = body.new_password

    if new_password == old_password:
        return JSONResponse(
            {"ok": False, "error": "New password must differ from the old password"},
            status_code=400,
        )

    from mediaman.auth.password_policy import password_issues

    issues = password_issues(new_password, username=admin)
    if issues:
        return JSONResponse(
            {
                "ok": False,
                "error": "Password does not meet the strength policy",
                "issues": issues,
            },
            status_code=400,
        )

    conn = get_db()
    if change_password(conn, admin, old_password, new_password):
        # Create a new session since the old ones were invalidated
        from mediaman.auth.rate_limit import get_client_ip
        from mediaman.auth.session import create_session

        new_token = create_session(
            conn,
            admin,
            user_agent=request.headers.get("user-agent", ""),
            client_ip=get_client_ip(request),
        )
        from mediaman.web.routes.auth import is_request_secure

        response = JSONResponse(
            {"ok": True, "message": "Password changed. You will be re-authenticated."}
        )
        set_session_cookie(response, new_token, secure=is_request_secure(request))
        return response
    logger.warning("password.change_rejected user=%s reason=wrong_old_password", admin)
    return JSONResponse({"ok": False, "error": "Current password is incorrect"}, status_code=403)


@router.get("/api/users/sessions")
def api_list_sessions(admin: str = Depends(get_current_admin)) -> dict[str, object]:
    """List active sessions for the current admin.

    Returns metadata only (timestamps, issued IP, fingerprint) — never
    raw tokens. Use `/api/users/sessions/revoke-others` to log out
    other devices.
    """
    conn = get_db()
    return {"sessions": list_sessions_for(conn, admin)}


@router.post("/api/users/sessions/revoke-others")
def api_revoke_other_sessions(
    request: Request,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Revoke every session for the current admin EXCEPT the current one.

    Useful after "I think my cookie leaked" — the admin keeps working
    on their current tab while every other device is logged out.
    """
    conn = get_db()
    # Delete all, then re-issue a session bound to the current request
    # (preserves continuity for the admin).
    destroyed = destroy_all_sessions_for(conn, admin)
    from mediaman.auth.rate_limit import get_client_ip
    from mediaman.auth.session import create_session

    new_token = create_session(
        conn,
        admin,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=get_client_ip(request),
    )
    from mediaman.web.routes.auth import is_request_secure

    response = JSONResponse(
        {
            "ok": True,
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
        ip=get_client_ip(request),
        detail={"revoked": destroyed},
    )
    return response
