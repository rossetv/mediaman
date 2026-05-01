"""User management API routes.

Handles listing, creating, deleting users, changing passwords, and managing
sessions for admin accounts.

Routes:
- GET  /api/users                      — list users
- POST /api/users                      — create user (requires recent reauth)
- DELETE /api/users/{user_id}          — delete user (requires reauth)
- POST /api/users/{user_id}/unlock     — admin unlock locked-out account (requires reauth)
- POST /api/users/change-password      — change own password
- GET  /api/users/sessions             — list own sessions
- POST /api/users/sessions/revoke-others — revoke other sessions
- POST /api/auth/reauth                — establish a recent-reauth marker for the session
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

from mediaman.audit import security_event, security_event_or_raise
from mediaman.auth.login_lockout import admin_unlock
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.auth.reauth import (
    _require_reauth,
    grant_recent_reauth,
    has_recent_reauth,
    reauth_window_seconds,
    revoke_all_reauth_for,
    revoke_reauth,
    verify_reauth_password,
)
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
# Reauth attempts have a per-actor rate cap. The namespace lockout in
# verify_reauth_password() is the main brute-force defence; this limiter
# is the per-minute throttle that keeps the lockout reachable without
# letting the attacker also burn an open-ended number of bcrypt cycles
# per minute. 30 / minute leaves head-room for the lockout to trip at
# 5 failures and then keep climbing toward the 10 / 15 escalation bands.
_REAUTH_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
# Password-change calls also share a per-actor burst cap. Same shape as
# the reauth limiter so the route sequence (reauth then change) is a
# coherent rate budget per session.
_PASSWORD_CHANGE_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)


class _CreateUserBody(BaseModel):
    """Body shape for POST /api/users."""

    username: str = ""
    password: str = ""


class _ChangePasswordBody(BaseModel):
    """Body shape for POST /api/users/change-password."""

    old_password: str = ""
    new_password: str = ""


class _ReauthBody(BaseModel):
    """Body shape for POST /api/auth/reauth."""

    password: str = ""


def _unauthorised_reauth() -> JSONResponse:
    """Build the canonical 'reauth required' response.

    Single helper so every privilege-establishing endpoint returns the
    exact same shape — the front-end can key off ``reauth_required: true``
    to prompt the user.
    """
    return JSONResponse(
        {
            "ok": False,
            "error": "Recent password re-authentication required",
            "reauth_required": True,
        },
        status_code=403,
    )


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
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Create a new admin user.

    Requires a recent reauth ticket (``POST /api/auth/reauth`` within
    the last :func:`reauth_window_seconds` seconds). Without it, a
    stolen session cookie cannot mint a permanent admin account that
    survives session rotation.
    """
    if not _USER_CREATE_LIMITER.check(admin):
        logger.warning("user.create_throttled actor=%s", admin)
        return JSONResponse(
            {"ok": False, "error": "Too many user-creation attempts — slow down"},
            status_code=429,
        )

    conn = get_db()
    if not has_recent_reauth(conn, session_token, admin):
        logger.warning("user.create_rejected actor=%s reason=reauth_required", admin)
        return _unauthorised_reauth()

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

    # Audit fail-closed: write the audit row and the create_user mutation
    # in one transaction so we never have a "user created but no audit
    # trail" outcome. ``create_user`` owns the BEGIN IMMEDIATE so the
    # bcrypt hash is computed before the writer lock is held.
    try:
        create_user(
            conn,
            username,
            password,
            audit_actor=admin,
            audit_ip=get_client_ip(request),
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
    except Exception:
        logger.exception("user.create failed actor=%s username=%s", admin, username)
        return JSONResponse(
            {"ok": False, "error": "Internal error during user creation"},
            status_code=500,
        )
    logger.info("user.created actor=%s new_user=%s", admin, username)
    return {"ok": True, "username": username}


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
    try:
        deleted = delete_user(
            conn,
            user_id,
            admin,
            audit_actor=admin,
            audit_ip=get_client_ip(request),
        )
    except Exception:
        logger.exception("user.delete failed actor=%s target_id=%d", admin, user_id)
        return JSONResponse(
            {"ok": False, "error": "Internal error during user deletion"},
            status_code=500,
        )
    if deleted:
        logger.info("user.deleted actor=%s target_id=%d", admin, user_id)
        return {"ok": True}
    return JSONResponse(
        {"ok": False, "error": "Cannot delete yourself or user not found"}, status_code=400
    )


@router.post("/api/users/{user_id}/unlock")
def api_unlock_user(
    user_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Clear the lockout for an admin account.

    Combats the M21 denial-of-service: an unauthenticated attacker can
    pound the login endpoint to keep an admin's account at the 24-hour
    lock window indefinitely. With this endpoint a fellow admin (after
    establishing a recent-reauth ticket) can clear the lock so the
    legitimate user can sign in again.

    Refuses to unlock yourself — if you can call this endpoint your
    session is already authenticated, so a self-unlock is a no-op that
    only obscures audit trails. Refuses to unlock unknown user IDs to
    avoid leaking which IDs exist.
    """
    if not _USER_MGMT_LIMITER.check(admin):
        return JSONResponse(
            {"ok": False, "error": "Too many user-management operations"},
            status_code=429,
        )
    conn = get_db()
    if not has_recent_reauth(conn, session_token, admin):
        logger.warning("user.unlock_rejected actor=%s reason=reauth_required", admin)
        return _unauthorised_reauth()

    row = conn.execute(
        "SELECT username FROM admin_users WHERE id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        return JSONResponse(
            {"ok": False, "error": "User not found"},
            status_code=404,
        )
    target_username = row["username"]
    if target_username == admin:
        return JSONResponse(
            {"ok": False, "error": "Cannot unlock yourself"},
            status_code=400,
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cleared = admin_unlock(conn, target_username)
            security_event_or_raise(
                conn,
                event="user.unlocked",
                actor=admin,
                ip=get_client_ip(request),
                detail={
                    "target_id": user_id,
                    "target_username": target_username,
                    "had_lock": cleared,
                },
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except Exception:
        logger.exception("user.unlock failed actor=%s target_id=%d", admin, user_id)
        return JSONResponse(
            {"ok": False, "error": "Internal error during unlock"},
            status_code=500,
        )
    logger.info("user.unlocked actor=%s target=%s had_lock=%s", admin, target_username, cleared)
    return {"ok": True, "had_lock": cleared}


@router.post("/api/users/change-password")
def api_change_password(
    request: Request,
    body: _ChangePasswordBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Change the current user's password.

    Per-actor throttling (``_PASSWORD_CHANGE_LIMITER``) caps burst
    attempts; the reauth-namespace lockout inside
    :func:`change_password` provides the bcrypt-grade brute-force
    defence.
    """
    if not _PASSWORD_CHANGE_LIMITER.check(admin):
        logger.warning("password.change_throttled actor=%s", admin)
        return JSONResponse(
            {"ok": False, "error": "Too many password-change attempts — slow down"},
            status_code=429,
        )

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
    client_ip = get_client_ip(request)
    if change_password(
        conn,
        admin,
        old_password,
        new_password,
        audit_actor=admin,
        audit_ip=client_ip,
        audit_event="password.changed",
    ):
        # Create a new session since the old ones were invalidated.
        from mediaman.auth.session import create_session

        new_token = create_session(
            conn,
            admin,
            user_agent=request.headers.get("user-agent", ""),
            client_ip=client_ip,
        )
        from mediaman.web.routes.auth import is_request_secure

        response = JSONResponse(
            {"ok": True, "message": "Password changed. You will be re-authenticated."}
        )
        set_session_cookie(response, new_token, secure=is_request_secure(request))
        return response
    logger.warning("password.change_rejected user=%s reason=wrong_old_password", admin)
    security_event(
        conn,
        event="password.change_failed",
        actor=admin,
        ip=client_ip,
        detail={"reason": "wrong_old_password"},
    )
    return JSONResponse({"ok": False, "error": "Current password is incorrect"}, status_code=403)


@router.post("/api/auth/reauth")
def api_reauth(
    request: Request,
    body: _ReauthBody,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """Establish a "recent reauth" ticket for the current session.

    Required prelude to any privilege-establishing endpoint. The ticket
    is bound to the SHA-256 hash of the caller's session token so:

    * Logout / session rotation cascades the ticket away.
    * A different session belonging to the same user does not benefit
      from this reauth.

    Wrong-password attempts feed the ``reauth:<admin>`` lockout
    namespace so a stolen session cookie cannot turn this endpoint into
    an offline-style password oracle.
    """
    if not _REAUTH_LIMITER.check(admin):
        logger.warning("reauth.throttled actor=%s", admin)
        return JSONResponse(
            {"ok": False, "error": "Too many reauth attempts — slow down"},
            status_code=429,
        )
    conn = get_db()
    if not session_token:
        # Belt-and-braces: get_current_admin would already have raised if
        # the token was missing, but guard anyway so we never persist a
        # ticket against an empty key.
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    if not verify_reauth_password(conn, admin, body.password):
        logger.warning("reauth.failed actor=%s ip=%s", admin, get_client_ip(request))
        security_event(
            conn,
            event="reauth.failed",
            actor=admin,
            ip=get_client_ip(request),
        )
        return JSONResponse(
            {"ok": False, "error": "Password is incorrect"},
            status_code=403,
        )

    grant_recent_reauth(conn, session_token, admin)
    logger.info("reauth.granted actor=%s ip=%s", admin, get_client_ip(request))
    security_event(
        conn,
        event="reauth.granted",
        actor=admin,
        ip=get_client_ip(request),
        detail={"window_seconds": reauth_window_seconds()},
    )
    return JSONResponse(
        {
            "ok": True,
            "expires_in_seconds": reauth_window_seconds(),
        }
    )


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
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """Revoke every session for the current admin EXCEPT the current one.

    Useful after "I think my cookie leaked" — the admin keeps working
    on their current tab while every other device is logged out. We
    also drop any reauth tickets bound to the destroyed sessions so
    they cannot be reused.
    """
    conn = get_db()
    # Drop reauth tickets for this user before destroying their sessions
    # so a thief who held a ticket cannot perform one last privileged
    # action between the destroy and the new-session create.
    revoke_all_reauth_for(conn, admin)
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
    # Belt-and-braces: drop any ticket that may have been re-granted
    # for the freshly issued token (none should exist, but it costs us
    # nothing to be sure the old ticket's data is gone).
    if session_token:
        revoke_reauth(conn, session_token)
    return response
