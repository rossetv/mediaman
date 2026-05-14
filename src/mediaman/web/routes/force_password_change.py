"""Force-password-change page — served to admins whose stored password
fails the current strength policy.

Flow:

1. Admin signs in with a weak password. ``auth_routes.login_submit``
   flips ``admin_users.must_change_password = 1`` and issues a
   normal session cookie.
2. ``ForcePasswordChangeMiddleware`` intercepts any subsequent
   request carrying that cookie and redirects to
   ``/force-password-change``.
3. This module renders the form, accepts the POST, validates old
   password + new password strength, rotates the credential, clears
   the flag, and redirects to the dashboard.

Design: matches ``login.html`` — dark "cinematic" surface card,
centred on a black background, using the ``login-*`` CSS classes so
the look is consistent. Strength rules are listed so the user knows
up front what they need.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.auth.password_hash import change_password
from mediaman.web.auth.password_policy import password_issues, policy_summary
from mediaman.web.cookies import is_request_secure, set_session_cookie

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-actor cap on force-change attempts. The reauth-namespace lockout
# inside change_password() throttles bcrypt-grade brute force. This cap is
# a thin extra layer that slows down a stolen-session attacker enough that
# the operator notices the audit-log noise first.
_FORCE_CHANGE_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)
#: Per-IP companion to :data:`_FORCE_CHANGE_LIMITER`. Caps source-of-traffic
#: just like the per-user bucket so a single attacker network cannot cycle
#: through usernames to evade the per-user cap.
_FORCE_CHANGE_IP_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)


def _resolve_session(request: Request) -> tuple[str, None] | tuple[None, RedirectResponse]:
    """Resolve the current session.

    Returns ``(username, None)`` when the session is valid, or
    ``(None, RedirectResponse)`` when the caller must be redirected
    (no session, or session invalid).
    """
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return None, resolved
    username, _conn = resolved
    return username, None


@router.get("/force-password-change", response_class=HTMLResponse)
def force_change_page(request: Request) -> Response:
    """Render the force-change form."""
    username, redirect = _resolve_session(request)
    if redirect is not None:
        return redirect

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "force_password_change.html",
        {
            "username": username,
            "policy": policy_summary(),
            "error": None,
            "issues": [],
        },
    )


def _validate_force_change_inputs(
    username: str,
    old_password: str,
    new_password: str,
    confirm_password: str,
) -> str | list[str] | None:
    """Run the field-presence, password-match, and strength guards.

    Returns ``None`` when the inputs are acceptable, an error string for
    the field-presence / mismatch failures, or a list of policy issues
    when the new password fails the strength policy. Strength is checked
    last — no point touching bcrypt if the input fails the cheaper
    guards first.
    """
    if not old_password or not new_password:
        return "Please fill in every field."

    if new_password != confirm_password:
        return "The two new passwords don't match."

    issues = password_issues(new_password, username=username)
    if issues:
        return issues

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

    Returns the new session token on success, or ``None`` when the old
    password was wrong (the failure is audited here before returning so
    the caller only has to re-render the form).

    ``change_password`` invalidates every session for the user, so a
    fresh one is minted here so the admin lands on the dashboard logged
    in under the new credential.
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
            username,
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
    """Accept the password change."""
    username, redirect = _resolve_session(request)
    if redirect is not None:
        return redirect
    # _resolve_session returns (str, None) | (None, Response); the early return
    # above leaves username as str, but mypy cannot narrow through union tuples.
    username = cast(str, username)

    templates = cast(Jinja2Templates, request.app.state.templates)
    conn = get_db()
    client_ip = get_client_ip(request)

    def _render(error: str | None = None, issues: list[str] | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "force_password_change.html",
            {
                "username": username,
                "policy": policy_summary(),
                "error": error,
                "issues": issues or [],
            },
        )

    if not _FORCE_CHANGE_LIMITER.check(username):
        logger.warning("force_password_change.throttled user=%s scope=user", username)
        return _render(error="Too many attempts — wait a moment and try again.")
    if not _FORCE_CHANGE_IP_LIMITER.check(client_ip):
        logger.warning(
            "force_password_change.throttled user=%s scope=ip ip=%s", username, client_ip
        )
        return _render(error="Too many attempts — wait a moment and try again.")

    validation = _validate_force_change_inputs(
        username, old_password, new_password, confirm_password
    )
    if isinstance(validation, list):
        return _render(issues=validation)
    if validation is not None:
        return _render(error=validation)

    try:
        new_token = _rotate_and_reissue(
            conn, username, old_password, new_password, client_ip, request
        )
    except ValueError:
        # Policy-enforced inside change_password — should be caught by
        # the earlier issues check, but belt-and-braces.
        return _render(error="Password does not meet the strength policy.")

    if new_token is None:
        return _render(error="Current password is incorrect.")

    response = RedirectResponse("/", status_code=302)
    set_session_cookie(response, new_token, secure=is_request_secure(request))
    logger.info("force_password_change.ok user=%s ip=%s", username, client_ip)
    return response
