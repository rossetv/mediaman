from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import change_password
from mediaman.web.auth.session_store import create_session
from mediaman.web.cookies import is_request_secure, set_session_cookie
from mediaman.web.models.users import ChangePasswordBody
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.plex_link import carry_attempt
from mediaman.web.routes.users.rate_limits import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    too_many,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/users/change-password")
def api_change_password(
    request: Request,
    body: ChangePasswordBody,
    admin: str = Depends(get_current_user),
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """Change the current user's password. Per-actor (_PASSWORD_CHANGE_LIMITER) and per-IP
    (_PASSWORD_CHANGE_IP_LIMITER) throttles stop a stolen cookie being replayed unbounded
    from one network across user buckets; the reauth-namespace lockout in change_password is
    the bcrypt-grade defence behind them.
    """
    request_ip = get_client_ip(request)
    reserved = _PASSWORD_CHANGE_LIMITER.reserve(admin)
    if reserved is None:
        logger.warning("password.change_throttled actor=%s scope=actor", _sanitise_log_field(admin))
        return too_many(_PASSWORD_CHANGE_LIMITER, admin)
    reserved_ip = _PASSWORD_CHANGE_IP_LIMITER.reserve(request_ip)
    if reserved_ip is None:
        logger.warning(
            "password.change_throttled actor=%s scope=ip ip=%s",
            _sanitise_log_field(admin),
            request_ip,
        )
        return too_many(_PASSWORD_CHANGE_IP_LIMITER, request_ip)

    def refuse_input(error: str, message: str, **extra: object) -> JSONResponse:
        _PASSWORD_CHANGE_LIMITER.release(admin, reserved)
        _PASSWORD_CHANGE_IP_LIMITER.release(request_ip, reserved_ip)
        return respond_err(error, status=400, message=message, **extra)

    old_password = body.old_password
    new_password = body.new_password

    if new_password == old_password:
        return refuse_input("same_password", "New password must differ from the old password")

    from mediaman.web.auth.password_policy import password_issues

    issues = password_issues(new_password, username=admin)
    if issues:
        return refuse_input(
            "weak_password", "Password does not meet the strength policy", issues=issues
        )

    conn = get_db()
    client_ip = request_ip
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
        new_token = create_session(
            conn,
            admin,
            user_agent=request.headers.get("user-agent", ""),
            client_ip=client_ip,
        )
        carry_attempt(session_token, new_token)
        response = respond_ok({"message": "Password changed. You will be re-authenticated."})
        set_session_cookie(response, new_token, secure=is_request_secure(request))
        return response
    logger.warning(
        "password.change_rejected user=%s reason=wrong_old_password", _sanitise_log_field(admin)
    )
    security_event(
        conn,
        event="password.change_failed",
        actor=admin,
        ip=client_ip,
        detail={"reason": "wrong_old_password"},
    )
    return respond_err("wrong_password", status=403, message="Current password is incorrect")
