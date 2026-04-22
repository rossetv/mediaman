"""Login and logout routes."""

import logging
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mediaman.auth.rate_limit import (
    RateLimiter,
    get_client_ip,
    peer_is_trusted,
    trusted_proxies,
)
from mediaman.auth.audit import security_event
from mediaman.auth.password_policy import is_strong
from mediaman.auth.session import (
    authenticate,
    create_session,
    destroy_session,
    set_must_change_password,
    user_must_change_password,
    validate_session,
)
from mediaman.db import get_db

logger = logging.getLogger("mediaman")

router = APIRouter()
_limiter = RateLimiter(max_attempts=5, window_seconds=60)


def is_request_secure(request: Request) -> bool:
    """Return True when the effective scheme is HTTPS.

    Resolution order:

    1. ``MEDIAMAN_FORCE_SECURE_COOKIES=true`` — unconditional yes.
    2. ``MEDIAMAN_FORCE_SECURE_COOKIES=false`` — unconditional no
       (development / plaintext loopback).
    3. Otherwise default to **secure**. Mediaman is intended to be
       served over HTTPS on any public deployment, and failing open
       to plaintext cookies is exactly the scenario that turns a
       misconfigured reverse proxy into session theft. The uvicorn
       ``proxy_headers`` / ``forwarded_allow_ips`` machinery already
       rewrites ``request.url.scheme`` to match ``X-Forwarded-Proto``
       when a trusted peer sets it, and the per-app override below
       is a belt-and-braces check: if the app genuinely sees an HTTP
       request AND the operator hasn't opted out, we STILL set the
       cookie Secure so it can't be sent on a plaintext loopback.
    """
    override = os.environ.get("MEDIAMAN_FORCE_SECURE_COOKIES", "").strip().lower()
    if override == "true":
        return True
    if override == "false":
        return False

    # Best-effort scheme detection: honour X-Forwarded-Proto from a
    # trusted peer if the uvicorn rewrite didn't already promote the
    # scheme (e.g. deployment didn't pass ``forwarded_allow_ips``).
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else None
    trusted = trusted_proxies()
    if peer_is_trusted(peer, trusted):
        forwarded_proto = (
            request.headers.get("x-forwarded-proto", "")
            .split(",")[0]
            .strip()
            .lower()
        )
        if forwarded_proto == "https":
            return True

    # Default to True on a public-facing app — operators who genuinely
    # need plaintext (localhost-only dev) can set
    # ``MEDIAMAN_FORCE_SECURE_COOKIES=false``.
    return True


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    client_ip = get_client_ip(request)
    if not _limiter.check(client_ip):
        templates = request.app.state.templates
        return templates.TemplateResponse(request, "login.html", {
            "error": "Too many attempts. Try again later.",
        })

    conn = get_db()
    if not authenticate(conn, username, password):
        logger.info("auth.login_failed user=%s ip=%s", username, client_ip)
        security_event(
            conn, event="login.failed", actor=username, ip=client_ip,
            detail={"reason": "bad_credentials"},
        )
        templates = request.app.state.templates
        return templates.TemplateResponse(request, "login.html", {
            "error": "Invalid username or password.",
        })

    user_agent = request.headers.get("user-agent", "")

    # Evaluate plaintext against the strength policy BEFORE we've
    # stashed it elsewhere. If it fails, flip the must-change-password
    # flag so the session-guard middleware funnels this user to the
    # force-change page after login. We still issue a session — the
    # force-change page needs authenticated access to update the
    # password. We do NOT log the password anywhere.
    weak_password = not is_strong(password, username=username)
    if weak_password:
        set_must_change_password(conn, username, True)
        logger.info(
            "auth.weak_password_detected user=%s ip=%s — flagged for rotation",
            username, client_ip,
        )
        security_event(
            conn, event="password.weak_detected", actor=username, ip=client_ip,
        )

    token = create_session(
        conn, username, user_agent=user_agent, client_ip=client_ip,
    )
    logger.info("auth.login_success user=%s ip=%s", username, client_ip)
    security_event(
        conn, event="login.success", actor=username, ip=client_ip,
        detail={"ua_hash": user_agent[:80], "force_rotation": weak_password},
    )
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        "session_token", token,
        httponly=True, samesite="strict", max_age=86400,
        secure=is_request_secure(request),
    )
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
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
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    conn = get_db()
    user_agent = request.headers.get("user-agent", "")
    client_ip = get_client_ip(request)
    username = validate_session(
        conn, token, user_agent=user_agent, client_ip=client_ip,
    )
    if username is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    destroy_session(conn, token)
    logger.info("logout: session terminated for user=%s", username)
    security_event(
        conn, event="logout", actor=username, ip=get_client_ip(request),
    )
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session_token")
    return response
