from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.audit import security_event
from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import change_password
from mediaman.web.auth.password_hash._change_password import (
    ChangeLock,
    change_lock,
    is_same_password,
)
from mediaman.web.auth.session_store import create_session, list_sessions_for
from mediaman.web.cookies import is_request_secure, set_session_cookie
from mediaman.web.models.users import ChangePasswordBody
from mediaman.web.repository import tv as tvs
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.plex_link import carry_attempt
from mediaman.web.routes.users.rate_limits import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    _wait_text,
    too_many,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: How each issue of `password_issues` starts, and the name its rule goes by.
_RULES = (
    ("Password is required", "required"),
    ("Password is too long", "too_long"),
    ("Must be at least", "length"),
    ("Avoid repeating", "repeats"),
    ("Must not contain the username", "username"),
    ("This password is on the common-password list", "common"),
    ("Mix at least", "mix"),
    ("Avoid sequential", "sequence"),
)


def rules_of(issues: Iterable[str]) -> list[str]:
    """The name of the rule behind each of *issues*, in their order."""
    return [
        next((rule for start, rule in _RULES if issue.startswith(start)), "other")
        for issue in issues
    ]


def lock_words(lock: ChangeLock) -> str:
    when = "later" if lock.seconds_left is None else f"in {_wait_text(lock.seconds_left)}"
    return f"Too many wrong tries. Try again {when}."


def _others_signed_in(conn: sqlite3.Connection, username: str, token: str | None) -> int:
    now = now_utc()
    return sum(
        not session["current"]
        and (ends := parse_iso_strict_utc(session["expires_at"])) is not None
        and ends > now
        for session in list_sessions_for(conn, username, current_token=token)
    )


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

    if is_same_password(new_password, old_password):
        return refuse_input(
            "same_password",
            "New password must differ from the old password",
            field="new_password",
            rules=["same"],
        )

    from mediaman.web.auth.password_policy import password_issues

    issues = password_issues(new_password, username=admin)
    if issues:
        return refuse_input(
            "weak_password",
            "Password does not meet the strength policy",
            issues=issues,
            field="new_password",
            rules=rules_of(issues),
        )

    conn = get_db()
    client_ip = request_ip
    # Read first: the change ends them.
    signed_out = _others_signed_in(conn, admin, session_token)
    unpaired = [tv.name for tv in tvs.owned(conn, admin)]
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
        response = respond_ok(
            {
                "message": "Password changed. You will be re-authenticated.",
                "signed_out": signed_out,
                "unpaired": unpaired,
            }
        )
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
    # Read after the try, so that the one that sets the lock says so, and the same whether the
    # password was right or wrong.
    lock = change_lock(conn, admin)
    if lock is not None:
        return respond_err(
            "wrong_password",
            status=403,
            message=lock_words(lock),
            field="old_password",
            locked=True,
            retry_after=lock.seconds_left,
        )
    return respond_err(
        "wrong_password",
        status=403,
        message="Current password is incorrect",
        field="old_password",
        locked=False,
    )
