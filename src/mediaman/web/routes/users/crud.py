from __future__ import annotations

import logging
import sqlite3
from typing import Literal

from fastapi import APIRouter, Cookie, Depends, Request
from pydantic import BaseModel, ConfigDict
from starlette.responses import Response

from mediaman.core.audit import security_event
from mediaman.db import get_db
from mediaman.services.plex_link import repository as plex_links
from mediaman.services.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.login_lockout import admin_unlock_with_audit
from mediaman.web.auth.middleware import get_current_admin, get_current_user
from mediaman.web.auth.password_hash import (
    EmailTakenError,
    LastAdminError,
    UserExistsError,
    create_user,
    delete_user,
    is_last_admin,
    list_users,
    set_user_email,
    set_user_role,
)
from mediaman.web.auth.password_policy import password_issues
from mediaman.web.auth.reauth import has_recent_reauth
from mediaman.web.auth.user_crud import find_username_by_user_id
from mediaman.web.models.users import CreateUserBody, UpdateEmailBody
from mediaman.web.repository.avatars import avatar_urls
from mediaman.web.responses import respond_err, respond_failure, respond_ok
from mediaman.web.routes.users.passwords import rules_of
from mediaman.web.routes.users.rate_limits import (
    _UNTICKETED_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
    too_many,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_LAST_ADMIN_MESSAGE = "There must be at least one admin. Make someone else an admin first."


class _CreateUserBody(CreateUserBody):
    # The API default stays admin so existing callers keep minting admins; the
    # Settings form always sends the role it shows (Viewer preselected).
    role: Literal["admin", "viewer"] = "admin"


class _UpdateRoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["admin", "viewer"]


def _reserve(
    request: Request,
    limiter: ActionRateLimiter,
    admin: str,
    session_token: str | None,
    action: str,
    *,
    audited: bool = False,
    target_id: int | None = None,
    what: str = "Too many changes.",
) -> float | Response:
    """Count one try of *action* against *limiter*, or return the refusal of it.

    Without a recent reauth ticket the action's own limit is only looked at, since the front-end
    asks first and comes back once it has the ticket; such tries have a limit of their own.
    """
    conn = get_db()
    actor = _sanitise_log_field(admin)
    ticketed = has_recent_reauth(conn, session_token, admin)
    if not ticketed and not _UNTICKETED_LIMITER.check(admin):
        refusal = too_many(_UNTICKETED_LIMITER, admin, "Too many tries.")
    else:
        reserved = limiter.reserve(admin)
        if reserved is not None and ticketed:
            return reserved
        if reserved is not None:
            limiter.release(admin, reserved)
            logger.warning("%s_rejected actor=%s reason=reauth_required", action, actor)
            return respond_err(
                "reauth_required",
                status=403,
                message="Recent password re-authentication required",
                reauth_required=True,
            )
        refusal = too_many(limiter, admin, what)
    logger.warning("%s_throttled actor=%s", action, actor)
    if audited:
        security_event(
            conn,
            event=f"{action}.rate_limited",
            actor=admin,
            ip=get_client_ip(request),
            detail=None if target_id is None else {"target_id": target_id},
        )
    return refusal


@router.get("/api/users")
def api_list_users(admin: str = Depends(get_current_admin)) -> dict[str, object]:
    conn = get_db()
    linked = {link["username"]: link["linked_at"] for link in plex_links.linked_users(conn)}
    people = list_users(conn)
    pictures = avatar_urls(conn, [user["id"] for user in people])
    users = [
        {
            **user,
            "plex_linked_at": linked.get(user["username"]),
            "avatar_url": pictures[user["id"]],
        }
        for user in people
    ]
    return {"users": users, "current": admin}


@router.post("/api/users")
def api_create_user(
    request: Request,
    body: _CreateUserBody,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Create a new user with the given role. Requires a recent reauth ticket, so a stolen
    session cookie cannot mint a permanent account that survives session rotation. The new
    user must choose their own password at first sign-in, since the admin knows this one.
    """
    reserved = _reserve(
        request,
        _USER_CREATE_LIMITER,
        admin,
        session_token,
        "user.create",
        what="Too many new users.",
    )
    if isinstance(reserved, Response):
        return reserved
    conn = get_db()

    username = body.username.strip()
    password = body.password

    if not username or len(username) < 3 or len(username) > 64:
        _USER_CREATE_LIMITER.release(admin, reserved)
        return respond_err(
            "invalid_username", status=400, message="Username must be between 3 and 64 characters"
        )

    issues = password_issues(password, username=username)
    if issues:
        _USER_CREATE_LIMITER.release(admin, reserved)
        return respond_err(
            "weak_password",
            status=400,
            message="Password does not meet the strength policy",
            issues=issues,
            field="password",
            rules=rules_of(issues),
        )

    # Refused before the costly hash, so that refusals which do not count stay cheap.
    if any(user["username"] == username for user in list_users(conn)):
        _USER_CREATE_LIMITER.release(admin, reserved)
        return respond_failure(f"User '{username}' already exists", status=409)

    # Audit fail-closed: write the audit row and the create_user mutation
    # in one transaction so we never have a "user created but no audit
    # trail" outcome. ``create_user`` owns the BEGIN IMMEDIATE so the
    # bcrypt hash is computed before the writer lock is held.
    try:
        create_user(
            conn,
            username,
            password,
            role=body.role,
            must_change_password=True,
            audit_actor=admin,
            audit_ip=get_client_ip(request),
        )
    except UserExistsError as e:
        return respond_failure(str(e), status=409)
    except sqlite3.Error:
        logger.exception(
            "user.create failed actor=%s username=%s",
            _sanitise_log_field(admin),
            _sanitise_log_field(username),
        )
        return respond_err(
            "internal_error", status=500, message="Internal error during user creation"
        )
    logger.info(
        "user.created actor=%s new_user=%s role=%s",
        _sanitise_log_field(admin),
        _sanitise_log_field(username),
        body.role,
    )
    return respond_ok({"username": username})


@router.delete("/api/users/{user_id}")
def api_delete_user(
    user_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Delete an admin user (not yourself). Requires a recent reauth ticket, so a stolen
    session cookie alone cannot delete other admins.
    """
    reserved = _reserve(
        request,
        _USER_MGMT_LIMITER,
        admin,
        session_token,
        "user.delete",
        audited=True,
        target_id=user_id,
    )
    if isinstance(reserved, Response):
        return reserved
    conn = get_db()
    client_ip = get_client_ip(request)
    target_username = find_username_by_user_id(conn, user_id)
    if target_username is not None and is_last_admin(conn, target_username):
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("last_admin", status=400, message=_LAST_ADMIN_MESSAGE)
    try:
        deleted = delete_user(
            conn,
            user_id,
            admin,
            audit_actor=admin,
            audit_ip=client_ip,
        )
    except sqlite3.Error:
        logger.exception(
            "user.delete failed actor=%s target_id=%d", _sanitise_log_field(admin), user_id
        )
        return respond_err(
            "internal_error", status=500, message="Internal error during user deletion"
        )
    if deleted:
        logger.info("user.deleted actor=%s target_id=%d", _sanitise_log_field(admin), user_id)
        return respond_ok()
    _USER_MGMT_LIMITER.release(admin, reserved)
    return respond_err(
        "cannot_delete", status=400, message="Cannot delete yourself or user not found"
    )


@router.patch("/api/users/{user_id}/role")
def api_set_user_role(
    user_id: int,
    body: _UpdateRoleBody,
    request: Request,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Change another user's role. Requires a recent reauth ticket, like delete: a stolen
    session cookie alone must not promote an account to admin. Refuses to change your own
    role and to demote the last admin.
    """
    reserved = _reserve(request, _USER_MGMT_LIMITER, admin, session_token, "user.role")
    if isinstance(reserved, Response):
        return reserved
    conn = get_db()
    client_ip = get_client_ip(request)

    target_username = find_username_by_user_id(conn, user_id)
    if target_username is None:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("not_found", status=404, message="User not found")
    if target_username == admin:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err(
            "cannot_change_own_role", status=400, message="You cannot change your own role"
        )
    try:
        set_user_role(conn, target_username, body.role, audit_actor=admin, audit_ip=client_ip)
    except LastAdminError:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("last_admin", status=400, message=_LAST_ADMIN_MESSAGE)
    except sqlite3.Error:
        logger.exception(
            "user.role failed actor=%s target_id=%d", _sanitise_log_field(admin), user_id
        )
        return respond_err("internal_error", status=500, message="Internal error changing role")
    logger.info(
        "user.role_changed actor=%s target=%s role=%s",
        _sanitise_log_field(admin),
        _sanitise_log_field(target_username),
        body.role,
    )
    return respond_ok({"role": body.role})


@router.patch("/api/users/me/email")
def api_set_my_email(
    body: UpdateEmailBody,
    request: Request,
    admin: str = Depends(get_current_user),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Set or clear the signed-in user's notification email. Requires a recent reauth ticket, so
    a stolen session cookie alone cannot redirect download alerts to an attacker's inbox.
    """
    reserved = _reserve(
        request, _USER_MGMT_LIMITER, admin, session_token, "user.email_update", audited=True
    )
    if isinstance(reserved, Response):
        return reserved
    conn = get_db()
    client_ip = get_client_ip(request)

    try:
        set_user_email(conn, admin, body.email, audit_actor=admin, audit_ip=client_ip)
    except ValueError as exc:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("invalid_email", status=400, message=str(exc))
    except EmailTakenError:
        # Counts, unlike other refused input: it tells that someone else has the address.
        logger.warning("user.email_update_rejected actor=%s", _sanitise_log_field(admin))
        return respond_err(
            "invalid_email",
            status=400,
            message="This address cannot be used here. Use another, or ask an admin.",
        )
    except sqlite3.Error:
        logger.exception("user.email_update failed actor=%s", _sanitise_log_field(admin))
        return respond_err("internal_error", status=500, message="Internal error updating email")

    logger.info(
        "user.email_updated actor=%s cleared=%s", _sanitise_log_field(admin), not body.email.strip()
    )
    return respond_ok()


@router.patch("/api/users/{user_id}/email")
def api_set_user_email(
    user_id: int,
    body: UpdateEmailBody,
    request: Request,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Set or clear another user's notification email. Requires a recent reauth ticket, like a
    role change. A newsletter subscriber's address may be given, another user's never.
    """
    reserved = _reserve(
        request,
        _USER_MGMT_LIMITER,
        admin,
        session_token,
        "user.email_update",
        audited=True,
        target_id=user_id,
    )
    if isinstance(reserved, Response):
        return reserved
    conn = get_db()
    client_ip = get_client_ip(request)

    target_username = find_username_by_user_id(conn, user_id)
    if target_username is None:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("not_found", status=404, message="User not found")
    if target_username == admin:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err(
            "cannot_change_own_email", status=400, message="Change your own email on your Profile"
        )
    try:
        set_user_email(
            conn,
            target_username,
            body.email,
            allow_subscriber=True,
            audit_actor=admin,
            audit_ip=client_ip,
        )
    except ValueError as exc:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("invalid_email", status=400, message=str(exc))
    except EmailTakenError:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("email_taken", status=409, message="Another user has this address.")
    except sqlite3.Error:
        logger.exception(
            "user.email_update failed actor=%s target_id=%d", _sanitise_log_field(admin), user_id
        )
        return respond_err("internal_error", status=500, message="Internal error updating email")
    email = body.email.strip() or None
    logger.info(
        "user.email_updated actor=%s target=%s cleared=%s",
        _sanitise_log_field(admin),
        _sanitise_log_field(target_username),
        email is None,
    )
    return respond_ok({"email": email})


@router.post("/api/users/{user_id}/unlock")
def api_unlock_user(
    user_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Clear the lockout for an admin account.

    An unauthenticated attacker can hammer the login endpoint to hold an admin at the
    24-hour lock indefinitely; this lets a fellow admin (after a recent reauth) clear it.
    Refuses to unlock yourself, since a caller with a session gains nothing and it only
    obscures audit trails, and refuses unknown user IDs so existing IDs do not leak.
    """
    reserved = _reserve(request, _USER_MGMT_LIMITER, admin, session_token, "user.unlock")
    if isinstance(reserved, Response):
        return reserved
    conn = get_db()

    # rationale: admin_users is the auth module's table; routes
    # read it through the sanctioned helper instead of a raw SELECT.
    target_username = find_username_by_user_id(conn, user_id)
    if target_username is None:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("not_found", status=404, message="User not found")
    if target_username == admin:
        _USER_MGMT_LIMITER.release(admin, reserved)
        return respond_err("cannot_unlock_self", status=400, message="Cannot unlock yourself")

    try:
        was_locked, _ = admin_unlock_with_audit(
            conn,
            target_username,
            audit_actor=admin,
            audit_ip=get_client_ip(request),
            target_id=user_id,
        )
    except sqlite3.Error:
        logger.exception(
            "user.unlock failed actor=%s target_id=%d", _sanitise_log_field(admin), user_id
        )
        return respond_err("internal_error", status=500, message="Internal error during unlock")
    logger.info(
        "user.unlocked actor=%s target=%s had_lock=%s",
        _sanitise_log_field(admin),
        _sanitise_log_field(target_username),
        was_locked,
    )
    return respond_ok({"had_lock": was_locked})
