"""User CRUD routes: list, create, delete, unlock, promote.

Routes
------
- GET  /api/users                  — list all admin users
- POST /api/users                  — create a new admin user (reauth required)
- DELETE /api/users/{user_id}      — delete a user (password confirmation required)
- POST /api/users/{user_id}/unlock — clear a locked-out account (reauth required)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, Header, Request
from starlette.responses import Response

from mediaman.core.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth.login_lockout import admin_unlock_with_audit
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.auth.password_hash import (
    create_user,
    delete_user,
    find_username_by_user_id,
    list_users,
)
from mediaman.web.auth.reauth import has_recent_reauth, verify_reauth_password
from mediaman.web.middleware.rate_limit import rate_limit
from mediaman.web.models.users import CreateUserBody
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.users.rate_limits import _USER_CREATE_LIMITER, _USER_MGMT_LIMITER

logger = logging.getLogger(__name__)

router = APIRouter()


def _unauthorised_reauth() -> Response:
    """Build the canonical 'reauth required' 403 response.

    Single helper so every privilege-establishing endpoint returns the
    exact same shape — the front-end can key off ``reauth_required: true``
    to prompt the user.
    """
    return respond_err(
        "reauth_required",
        status=403,
        message="Recent password re-authentication required",
        reauth_required=True,
    )


@router.get("/api/users")
def api_list_users(admin: str = Depends(get_current_admin)) -> dict[str, object]:
    """List all admin users."""
    conn = get_db()
    return {"users": list_users(conn), "current": admin}


@router.post("/api/users")
@rate_limit(_USER_CREATE_LIMITER, key="actor")
def api_create_user(
    request: Request,
    body: CreateUserBody,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Create a new admin user.

    Requires a recent reauth ticket (``POST /api/auth/reauth`` within
    the last :func:`~mediaman.auth.reauth.reauth_window_seconds` seconds).
    Without it, a stolen session cookie cannot mint a permanent admin
    account that survives session rotation.
    """
    conn = get_db()
    if not has_recent_reauth(conn, session_token, admin):
        logger.warning("user.create_rejected actor=%s reason=reauth_required", admin)
        return _unauthorised_reauth()

    username = body.username.strip()
    password = body.password

    if not username or len(username) < 3 or len(username) > 64:
        return respond_err(
            "invalid_username", status=400, message="Username must be between 3 and 64 characters"
        )

    from mediaman.web.auth.password_policy import password_issues

    issues = password_issues(password, username=username)
    if issues:
        return respond_err(
            "weak_password",
            status=400,
            message="Password does not meet the strength policy",
            issues=issues,
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
        return respond_err(str(e), status=409)
    except Exception:
        logger.exception("user.create failed actor=%s username=%s", admin, username)
        return respond_err(
            "internal_error", status=500, message="Internal error during user creation"
        )
    logger.info("user.created actor=%s new_user=%s", admin, username)
    return respond_ok({"username": username})


@router.delete("/api/users/{user_id}")
def api_delete_user(
    user_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
    x_confirm_password: str | None = Header(default=None),
) -> Response:
    """Delete an admin user. Cannot delete yourself.

    Requires the caller's password via the ``X-Confirm-Password`` request
    header. Passing the password in the query string is explicitly rejected —
    query strings appear in access logs, proxies, and browser history, making
    them unsuitable for credentials.

    A compromised session cookie alone cannot delete other admins.
    """
    # Reject any attempt to pass the password via query string — it leaks
    # into access logs and server-side request recording.
    if "confirm_password" in request.query_params:
        return respond_err(
            "use_header",
            status=400,
            message="confirm_password must be sent via X-Confirm-Password header, not the query string",
        )

    conn = get_db()
    client_ip = get_client_ip(request)
    if not _USER_MGMT_LIMITER.check(admin):
        logger.warning("user.delete_throttled actor=%s target_id=%d", admin, user_id)
        security_event(
            conn,
            event="user.delete.rate_limited",
            actor=admin,
            ip=client_ip,
            detail={"target_id": user_id},
        )
        return respond_err(
            "too_many_requests", status=429, message="Too many user-management operations"
        )
    pw = x_confirm_password or ""
    # ``verify_reauth_password`` feeds wrong-password attempts into the
    # ``reauth:<admin>`` namespace lockout so a stolen session cookie cannot
    # be used to brute-force the password through this endpoint — the same
    # 5/10/15 escalation that gates plain login also gates this delete.
    if not verify_reauth_password(conn, admin, pw):
        logger.warning("user.delete_rejected user=%s reason=reauth_required", admin)
        security_event(
            conn,
            event="user.delete.reauth_failed",
            actor=admin,
            ip=client_ip,
            detail={"target_id": user_id},
        )
        return respond_err(
            "password_required", status=403, message="Password confirmation required"
        )
    try:
        deleted = delete_user(
            conn,
            user_id,
            admin,
            audit_actor=admin,
            audit_ip=client_ip,
        )
    except Exception:
        logger.exception("user.delete failed actor=%s target_id=%d", admin, user_id)
        return respond_err(
            "internal_error", status=500, message="Internal error during user deletion"
        )
    if deleted:
        logger.info("user.deleted actor=%s target_id=%d", admin, user_id)
        return respond_ok()
    return respond_err(
        "cannot_delete", status=400, message="Cannot delete yourself or user not found"
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
        return respond_err(
            "too_many_requests", status=429, message="Too many user-management operations"
        )
    conn = get_db()
    if not has_recent_reauth(conn, session_token, admin):
        logger.warning("user.unlock_rejected actor=%s reason=reauth_required", admin)
        return _unauthorised_reauth()

    target_username = find_username_by_user_id(conn, user_id)
    if target_username is None:
        return respond_err("not_found", status=404, message="User not found")
    if target_username == admin:
        return respond_err("cannot_unlock_self", status=400, message="Cannot unlock yourself")

    try:
        cleared = admin_unlock_with_audit(
            conn,
            target_username,
            audit_actor=admin,
            audit_ip=get_client_ip(request),
            target_id=user_id,
        )
    except Exception:
        logger.exception("user.unlock failed actor=%s target_id=%d", admin, user_id)
        return respond_err("internal_error", status=500, message="Internal error during unlock")
    logger.info("user.unlocked actor=%s target=%s had_lock=%s", admin, target_username, cleared)
    return respond_ok({"had_lock": cleared})
