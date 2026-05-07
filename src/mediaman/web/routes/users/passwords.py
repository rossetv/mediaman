"""Password-management routes.

Routes
------
- POST /api/users/change-password — change the current user's own password
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.auth.password_hash import change_password
from mediaman.web.auth.session_store import create_session
from mediaman.web.models.users import ChangePasswordBody
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes._helpers import set_session_cookie
from mediaman.web.routes.users.rate_limits import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/users/change-password")
def api_change_password(
    request: Request,
    body: ChangePasswordBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Change the current user's password.

    Per-actor throttling (:data:`_PASSWORD_CHANGE_LIMITER`) caps burst
    attempts by username; per-IP throttling
    (:data:`_PASSWORD_CHANGE_IP_LIMITER`) caps attempts by source so a
    stolen cookie cannot be replayed unbounded from a single attacker
    network even if it bounces across user buckets. The reauth-namespace
    lockout inside :func:`~mediaman.auth.session.change_password` provides
    the bcrypt-grade brute-force defence behind both limiters.
    """
    request_ip = get_client_ip(request)
    if not _PASSWORD_CHANGE_LIMITER.check(admin):
        logger.warning("password.change_throttled actor=%s scope=actor", admin)
        return respond_err(
            "too_many_requests", status=429, message="Too many password-change attempts — slow down"
        )
    if not _PASSWORD_CHANGE_IP_LIMITER.check(request_ip):
        logger.warning("password.change_throttled actor=%s scope=ip ip=%s", admin, request_ip)
        return respond_err(
            "too_many_requests", status=429, message="Too many password-change attempts — slow down"
        )

    old_password = body.old_password
    new_password = body.new_password

    if new_password == old_password:
        return respond_err(
            "same_password", status=400, message="New password must differ from the old password"
        )

    from mediaman.web.auth.password_policy import password_issues

    issues = password_issues(new_password, username=admin)
    if issues:
        return respond_err(
            "weak_password",
            status=400,
            message="Password does not meet the strength policy",
            issues=issues,
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
        from mediaman.web.routes.auth import is_request_secure

        new_token = create_session(
            conn,
            admin,
            user_agent=request.headers.get("user-agent", ""),
            client_ip=client_ip,
        )
        response = respond_ok({"message": "Password changed. You will be re-authenticated."})
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
    return respond_err("wrong_password", status=403, message="Current password is incorrect")
