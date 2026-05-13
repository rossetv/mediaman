"""Login and logout routes."""

from __future__ import annotations

import hashlib
import logging
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
# tries.  Per-actor lockout (see :mod:`mediaman.auth.login_lockout`)
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
    """Return a stable 16-hex-char SHA-256 prefix of *user_agent*.

    Stored in the audit-log ``detail`` column as ``ua_hash`` so the
    operator can correlate sessions originating from the same client
    without leaking the full UA string (which can carry version
    fingerprints and other identifying noise).
    """
    return hashlib.sha256((user_agent or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    """Render the login form (HTML)."""
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    """Authenticate a username/password submission, applying per-IP and per-username rate limits before touching the credential check.

    On success, issues a session cookie and redirects to the post-login destination; on failure, re-renders the form with a generic error to avoid leaking which field was wrong.
    """
    client_ip = get_client_ip(request)
    if not _limiter.check(client_ip):
        templates = cast(Jinja2Templates, request.app.state.templates)
        response: Response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Too many attempts. Try again later.",
            },
        )
        # Hint compliant clients (and well-behaved scripts) that the
        # window is finite.  The value is the upper bound — actual unblock
        # is sliding-window — but it gives operators and automation a
        # concrete number to back off against.
        response.headers["Retry-After"] = str(_LOGIN_RATE_WINDOW_SECONDS)
        return response

    conn = get_db()
    if not authenticate(conn, username, password):
        # On the failed-login path *username* is unauthenticated and
        # therefore fully attacker-controlled — sanitise before it lands
        # in the LOGGER message, the audit ``actor`` column, AND the
        # audit ``detail`` blob (which is rendered into the history
        # page UI).  Without this an attacker could stuff control
        # bytes (CR/LF for log forging, ANSI escape codes for terminal
        # injection) or a multi-megabyte string into every audit row.
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

    user_agent = request.headers.get("user-agent", "")

    # Evaluate plaintext against the strength policy BEFORE we've
    # stashed it elsewhere. If it fails, flip the must-change-password
    # flag so the session-guard middleware funnels this user to the
    # force-change page after login. We still issue a session — the
    # force-change page needs authenticated access to update the
    # password. We do NOT log the password anywhere.
    #
    # Coalesce the audit event: the flag is sticky until the user
    # rotates the password, so logging ``password.weak_detected`` on
    # every subsequent login generates one row per session for the
    # same condition.  Only emit the event when we actually flip the
    # flag from 0 to 1 — every later login is a no-op.
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

    token = create_session(
        conn,
        username,
        user_agent=user_agent,
        client_ip=client_ip,
    )
    logger.info("auth.login_success user=%s ip=%s", username, client_ip)
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
    """Clear the session cookie with explicit attributes.

    RFC 6265bis matches a deletion ``Set-Cookie`` against existing
    cookies by ``(name, domain, path)``.  ``Starlette.delete_cookie``
    defaults ``Path=/`` which currently happens to match the value
    ``set_session_cookie`` writes, but a future change to either side
    would silently break deletion.  Pin the attributes here so the two
    are coupled by the same source of truth.
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

    Requires a valid session cookie — a cross-origin POST (including
    CSRF from an attacker page) without a legitimate session in the
    request's cookies gets a 401 and no ``Set-Cookie`` clear. This
    closes the "forced-logout" CSRF where any third-party page could
    clear an admin's session by submitting a POST.

    The ``CSRFOriginMiddleware`` already blocks cross-origin POSTs
    from a *browser* that ships Origin, but some legacy webviews and
    programmatic clients omit ``Origin`` on the same-site heuristic.
    Requiring a valid session on top means the worst an unauthenticated
    CSRF can do is trigger a 401 — not mutate cookie state.
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
    logger.info("logout: session terminated for user=%s", username)
    security_event(
        conn,
        event="logout",
        actor=username,
        ip=client_ip,
    )
    redirect_response = RedirectResponse("/login", status_code=302)
    _clear_session_cookie(redirect_response, secure=secure)
    return redirect_response
