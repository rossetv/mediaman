"""Force-password-change page, served to admins whose stored password fails the current
strength policy.

login_submit flips admin_users.must_change_password to 1 and issues a normal session;
ForcePasswordChangeMiddleware then redirects every request with that cookie here. This
module validates the old and new password, rotates the credential, clears the flag and
redirects to Watch.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import NamedTuple, cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import LANDING_PATH, resolve_page_session
from mediaman.web.auth.password_hash import change_password
from mediaman.web.auth.password_hash._change_password import change_lock, is_same_password
from mediaman.web.auth.password_policy import password_issues, policy_summary
from mediaman.web.cookies import is_request_secure, set_session_cookie
from mediaman.web.routes.users.passwords import lock_words, rules_of
from mediaman.web.routes.users.rate_limits import _wait_text

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-actor cap on force-change attempts. The reauth-namespace lockout
# inside change_password() throttles bcrypt-grade brute force. This cap is
# a thin extra layer that slows down a stolen-session attacker enough that
# the operator notices the audit-log noise first.
_FORCE_CHANGE_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)
#: Per-IP companion to _FORCE_CHANGE_LIMITER. Caps source-of-traffic
#: just like the per-user bucket so a single attacker network cannot cycle
#: through usernames to evade the per-user cap.
_FORCE_CHANGE_IP_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)


def _resolve_session(request: Request) -> tuple[str, None] | tuple[None, RedirectResponse]:
    """Return (username, None) for a valid session, or (None, RedirectResponse) when the caller
    must be redirected.
    """
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return None, resolved
    username, _conn = resolved
    return username, None


class _Refusal(NamedTuple):
    """Why the page comes back: in words, by rule name, and the field it is about, if one is."""

    error: str | None = None
    field: str | None = None
    issues: list[str] | None = None
    rules: list[str] | None = None
    locked: bool = False


def _throttled(limiter: ActionRateLimiter, actor: str) -> _Refusal:
    return _Refusal(f"Too many attempts. Try again in {_wait_text(limiter.retry_after(actor))}.")


def _render(request: Request, username: str | None, refusal: _Refusal) -> HTMLResponse:
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "force_password_change.html",
        {
            "username": username,
            "policy": policy_summary(),
            "error": refusal.error,
            "issues": refusal.issues or [],
            "field": refusal.field,
            "rules": refusal.rules or [],
            "locked": refusal.locked,
        },
    )


@router.get("/force-password-change", response_class=HTMLResponse)
def force_change_page(request: Request) -> Response:
    username, redirect = _resolve_session(request)
    if redirect is not None:
        return redirect

    return _render(request, username, _Refusal())


def _validate_force_change_inputs(
    username: str,
    old_password: str,
    new_password: str,
    confirm_password: str,
) -> _Refusal | None:
    """Run the presence, match, reuse and strength guards. Returns None when acceptable. The old
    password is not checked here, so that bcrypt is not touched when a cheaper guard fails.
    """
    if not old_password or not new_password:
        empty = "new_password" if old_password else "old_password"
        return _Refusal("Please fill in every field.", empty)

    if new_password != confirm_password:
        return _Refusal("The two new passwords don't match.", "confirm_password")

    # Only what was typed is compared: what is typed as the old one is checked before any change.
    if is_same_password(new_password, old_password):
        return _Refusal(
            "New password must differ from the old password.", "new_password", rules=["same"]
        )

    issues = password_issues(new_password, username=username)
    if issues:
        return _Refusal(field="new_password", issues=issues, rules=rules_of(issues))

    return None


def _rotate_and_reissue(
    conn: sqlite3.Connection,
    username: str,
    old_password: str,
    new_password: str,
    client_ip: str,
    request: Request,
) -> str | None:
    """Rotate the credential via bcrypt and mint a fresh session.

    Returns the new session token, or None when the old password was wrong (audited here).
    change_password invalidates every session for the user, so a fresh one is minted to keep
    the admin logged in.
    """
    ok = change_password(
        conn,
        username,
        old_password,
        new_password,
        audit_actor=username,
        audit_ip=client_ip,
        audit_event="password.force_changed",
    )

    if not ok:
        logger.info(
            "force_password_change.wrong_old user=%s ip=%s",
            _sanitise_log_field(username),
            client_ip,
        )
        security_event(
            conn,
            event="password.force_change_failed",
            actor=username,
            ip=client_ip,
            detail={"reason": "wrong_old_password"},
        )
        return None

    from mediaman.web.auth.session_store import (
        create_session,  # late import: test patches the owning module
    )

    return create_session(
        conn,
        username,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=client_ip,
    )


@router.post("/force-password-change", response_class=HTMLResponse)
def force_change_submit(
    request: Request,
    old_password: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
) -> Response:
    username, redirect = _resolve_session(request)
    if redirect is not None:
        return redirect
    # _resolve_session returns (str, None) | (None, Response); the early return
    # above leaves username as str, but mypy cannot narrow through union tuples.
    username = cast(str, username)

    conn = get_db()
    client_ip = get_client_ip(request)

    def refuse(refusal: _Refusal) -> HTMLResponse:
        return _render(request, username, refusal)

    if not _FORCE_CHANGE_LIMITER.check(username):
        logger.warning(
            "force_password_change.throttled user=%s scope=user", _sanitise_log_field(username)
        )
        return refuse(_throttled(_FORCE_CHANGE_LIMITER, username))
    if not _FORCE_CHANGE_IP_LIMITER.check(client_ip):
        logger.warning(
            "force_password_change.throttled user=%s scope=ip ip=%s",
            _sanitise_log_field(username),
            client_ip,
        )
        return refuse(_throttled(_FORCE_CHANGE_IP_LIMITER, client_ip))

    refusal = _validate_force_change_inputs(username, old_password, new_password, confirm_password)
    if refusal is not None:
        return refuse(refusal)

    try:
        new_token = _rotate_and_reissue(
            conn, username, old_password, new_password, client_ip, request
        )
    except ValueError:
        # Policy-enforced inside change_password — should be caught by
        # the earlier issues check, but belt-and-braces.
        return refuse(_Refusal("Password does not meet the strength policy.", "new_password"))

    if new_token is None:
        # Read after the try, so that the one that sets the lock says so.
        lock = change_lock(conn, username)
        if lock is not None:
            return refuse(_Refusal(lock_words(lock), "old_password", locked=True))
        return refuse(_Refusal("Current password is incorrect.", "old_password"))

    response = RedirectResponse(LANDING_PATH, status_code=302)
    set_session_cookie(response, new_token, secure=is_request_secure(request))
    logger.info("force_password_change.ok user=%s ip=%s", _sanitise_log_field(username), client_ip)
    return response
