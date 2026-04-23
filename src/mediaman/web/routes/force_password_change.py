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

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.audit import security_event
from mediaman.auth.middleware import resolve_page_session
from mediaman.auth.password_policy import password_issues, policy_summary
from mediaman.auth.rate_limit import get_client_ip
from mediaman.auth.session import change_password
from mediaman.db import get_db
from mediaman.web.routes._helpers import set_session_cookie

logger = logging.getLogger("mediaman")

router = APIRouter()


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


# One-line shim preserving the old name for any out-of-scope caller that
# may reference it directly. Scheduled for removal in the next refactor.
_require_flagged_admin = _resolve_session


@router.get("/force-password-change", response_class=HTMLResponse)
def force_change_page(request: Request) -> Response:
    """Render the force-change form."""
    username, redirect = _resolve_session(request)
    if redirect is not None:
        return redirect

    templates = request.app.state.templates
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

    templates = request.app.state.templates
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

    if not old_password or not new_password:
        return _render(error="Please fill in every field.")

    if new_password != confirm_password:
        return _render(error="The two new passwords don't match.")

    # Validate strength first — no point touching bcrypt if the input
    # fails the policy.
    issues = password_issues(new_password, username=username)
    if issues:
        return _render(issues=issues)

    try:
        ok = change_password(conn, username, old_password, new_password)
    except ValueError:
        # Policy-enforced inside change_password — should be caught by
        # the earlier issues check, but belt-and-braces.
        return _render(error="Password does not meet the strength policy.")

    if not ok:
        logger.info(
            "force_password_change.wrong_old user=%s ip=%s", username, client_ip,
        )
        security_event(
            conn, event="password.force_change_failed", actor=username, ip=client_ip,
            detail={"reason": "wrong_old_password"},
        )
        return _render(error="Current password is incorrect.")

    # change_password invalidates every session for the user; we need
    # to issue a fresh one so the admin lands on the dashboard logged
    # in under the new credential.
    from mediaman.auth.session import create_session
    new_token = create_session(
        conn, username,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=client_ip,
    )

    from mediaman.web.routes.auth import is_request_secure

    response = RedirectResponse("/", status_code=302)
    set_session_cookie(response, new_token, secure=is_request_secure(request))
    logger.info("force_password_change.ok user=%s ip=%s", username, client_ip)
    security_event(
        conn, event="password.force_changed", actor=username, ip=client_ip,
    )
    return response
