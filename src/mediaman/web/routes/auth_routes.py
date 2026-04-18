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

    ``X-Forwarded-Proto`` is only trusted when ``request.client.host``
    falls within ``MEDIAMAN_TRUSTED_PROXIES`` — otherwise any client can
    downgrade cookie protection simply by setting the header. Operators
    can force-on by setting ``MEDIAMAN_FORCE_SECURE_COOKIES=true`` when
    the app is *always* served behind HTTPS.
    """
    if os.environ.get("MEDIAMAN_FORCE_SECURE_COOKIES", "").lower() == "true":
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
        if forwarded_proto:
            return forwarded_proto == "https"
    return request.url.scheme == "https"


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
