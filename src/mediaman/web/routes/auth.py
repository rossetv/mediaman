from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.audit import security_event
from mediaman.db import get_db
from mediaman.services.rate_limit import (
    RateLimiter,
    get_client_ip,
)
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.password_hash import (
    authenticate,
    set_must_change_password,
    user_must_change_password,
)
from mediaman.web.auth.password_policy import is_strong
from mediaman.web.auth.session_store import (
    create_session,
    destroy_session,
    validate_session,
)
from mediaman.web.cookies import is_request_secure, set_session_cookie
from mediaman.web.responses import respond_err

logger = logging.getLogger(__name__)

router = APIRouter()
# Login bucket: 5 attempts per 5 minutes per /24 IPv4 block (or /64 IPv6).
# CGNAT lumps ~250 customers behind a single /24, so a 60-second window
# would cap legitimate concurrent attempts at one per 12 seconds across
# the whole pool.  A 300-second window keeps the bucket large enough that
# real users with fat-finger typos still get through while a credential-
# stuffing burst from one network gets shut down within the first few
# tries.  Per-actor lockout (see mediaman.web.auth.login_lockout)
# handles slow grinding distinct from this fast-burst defence.
#
# CAPTCHA-after-N-failures gate (out of scope here): a follow-up could
# layer a hCaptcha/Turnstile challenge once a bucket trips this limit so
# a legitimate user is never locked out for the full window.
_LOGIN_RATE_WINDOW_SECONDS = 300
_limiter = RateLimiter(max_attempts=5, window_seconds=_LOGIN_RATE_WINDOW_SECONDS)

# Audit-log usernames are stored verbatim in the ``actor`` column for
# operator-friendly grep, but the ``detail`` blob is rendered into the
# history page UI.  Cap attacker-controlled usernames in the audit detail
# at this many chars to bound the log-row size and prevent a megabyte-
# username from polluting the history page.
_AUDIT_USERNAME_LIMIT = 64


def _ua_hash(user_agent: str) -> str:
    """Return a stable 16-hex SHA-256 prefix of user_agent, so the audit log can correlate
    sessions without storing the full UA string.
    """
    return hashlib.sha256((user_agent or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(request, "login.html", {"error": None})


def _render_rate_limited(request: Request) -> Response:
    """Re-render the login form with the throttle message and a Retry-After hint. The value is
    an upper bound: the actual unblock is sliding-window.
    """
    templates = cast(Jinja2Templates, request.app.state.templates)
    response: Response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "Too many attempts. Try again later.",
        },
    )
    response.headers["Retry-After"] = str(_LOGIN_RATE_WINDOW_SECONDS)
    return response


def _handle_failed_login(
    conn: sqlite3.Connection, username: str, client_ip: str, request: Request
) -> Response:
    """Audit a failed credential check and re-render the form generically.

    username is attacker-controlled here: sanitise it before it reaches the log message, the
    audit actor column and the audit detail blob (rendered in the history UI), so control
    bytes (log forging, ANSI escapes) and huge strings cannot land in every audit row.
    """
    safe_username = _sanitise_log_field(username, limit=_AUDIT_USERNAME_LIMIT)
    logger.info("auth.login_failed user=%s ip=%s", safe_username, client_ip)
    security_event(
        conn,
        event="login.failed",
        actor=safe_username,
        ip=client_ip,
        detail={"reason": "bad_credentials"},
    )
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "Invalid username or password.",
        },
    )


def _flag_weak_password(
    conn: sqlite3.Connection, username: str, password: str, client_ip: str
) -> bool:
    """Flip the must-change-password flag when the plaintext fails the policy; return whether
    it was weak.

    The session is still issued, since the force-change page needs it. The audit event is
    emitted only when the flag flips from 0 to 1, because the flag is sticky and would
    otherwise log one row per login.
    """
    weak_password = not is_strong(password, username=username)
    if weak_password:
        already_flagged = user_must_change_password(conn, username)
        set_must_change_password(conn, username, True)
        if not already_flagged:
            logger.info(
                "auth.weak_password_detected user=%s ip=%s — flagged for rotation",
                username,
                client_ip,
            )
            security_event(
                conn,
                event="password.weak_detected",
                actor=username,
                ip=client_ip,
            )
    return weak_password


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    client_ip = get_client_ip(request)
    if not _limiter.check(client_ip):
        return _render_rate_limited(request)

    conn = get_db()
    if not authenticate(conn, username, password, client_ip=client_ip):
        return _handle_failed_login(conn, username, client_ip, request)

    user_agent = request.headers.get("user-agent", "")
    weak_password = _flag_weak_password(conn, username, password, client_ip)

    token = create_session(
        conn,
        username,
        user_agent=user_agent,
        client_ip=client_ip,
    )
    logger.info("auth.login_success user=%s ip=%s", _sanitise_log_field(username), client_ip)
    security_event(
        conn,
        event="login.success",
        actor=username,
        ip=client_ip,
        # ``ua_hash`` is a SHA-256 hash prefix so a long attacker-controlled
        # UA cannot pollute the audit row.
        detail={"ua_hash": _ua_hash(user_agent), "force_rotation": weak_password},
    )
    response = RedirectResponse("/", status_code=302)
    set_session_cookie(response, token, secure=is_request_secure(request))
    return response


def _clear_session_cookie(response: Response, *, secure: bool) -> None:
    """Clear the session cookie with explicit attributes. A deletion Set-Cookie matches by
    (name, domain, path), so pin them here to the same source of truth as
    set_session_cookie.
    """
    response.delete_cookie(
        "session_token",
        path="/",
        samesite="strict",
        secure=secure,
        httponly=True,
    )


@router.post("/api/auth/logout")
def logout(request: Request) -> Response:
    """Log out the current session.

    Requires a valid session cookie, so a cross-origin POST without one gets a 401 and no
    Set-Cookie clear (closes forced-logout CSRF). The Origin middleware alone is not enough:
    some legacy webviews and programmatic clients omit Origin.
    """
    token = request.cookies.get("session_token")
    if not token:
        return respond_err("not_authenticated", status=401)
    conn = get_db()
    user_agent = request.headers.get("user-agent", "")
    client_ip = get_client_ip(request)
    username = validate_session(
        conn,
        token,
        user_agent=user_agent,
        client_ip=client_ip,
        request_supplied=True,
    )
    secure = is_request_secure(request)
    if username is None:
        # Stale / forged token — return 401 and clear the cookie so the
        # browser doesn't keep sending a known-bad token on every
        # subsequent request.  Unauthenticated CSRF still hits the
        # earlier branch (no token at all), so this only fires for the
        # legitimate "expired session" case.
        stale_response = respond_err("not_authenticated", status=401)
        _clear_session_cookie(stale_response, secure=secure)
        return stale_response
    destroy_session(conn, token)
    logger.info("logout: session terminated for user=%s", _sanitise_log_field(username))
    security_event(
        conn,
        event="logout",
        actor=username,
        ip=client_ip,
    )
    redirect_response = RedirectResponse("/login", status_code=302)
    _clear_session_cookie(redirect_response, secure=secure)
    return redirect_response
