"""Login and logout routes."""

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mediaman.auth.rate_limit import (
    RateLimiter,
    _peer_is_trusted,
    _trusted_proxies,
    get_client_ip,
)
from mediaman.auth.session import authenticate, create_session, destroy_session
from mediaman.db import get_db

router = APIRouter()
_limiter = RateLimiter(max_attempts=5, window_seconds=60)


def _is_request_secure(request: Request) -> bool:
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
    trusted = _trusted_proxies()
    if _peer_is_trusted(peer, trusted):
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
        templates = request.app.state.templates
        return templates.TemplateResponse(request, "login.html", {
            "error": "Invalid username or password.",
        })

    token = create_session(conn, username)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        "session_token", token,
        httponly=True, samesite="strict", max_age=86400,
        secure=_is_request_secure(request),
    )
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        conn = get_db()
        destroy_session(conn, token)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session_token")
    return response
