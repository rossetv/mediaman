"""FastAPI auth dependencies."""

from fastapi import Cookie, HTTPException, Request
from starlette.responses import RedirectResponse

from mediaman.auth.rate_limit import get_client_ip
from mediaman.auth.session import validate_session
from mediaman.db import get_db


def get_current_admin(
    request: Request,
    session_token: str | None = Cookie(default=None),
) -> str:
    """FastAPI dependency — returns username or raises 401.

    Passes the current request's ``User-Agent`` and client IP into
    ``validate_session`` so session fingerprint binding is enforced on
    every request. Uniform ``Not authenticated`` error (no separate
    "Session expired" leak).
    """
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    user_agent = request.headers.get("user-agent", "")
    client_ip = get_client_ip(request)
    username = validate_session(
        conn, session_token, user_agent=user_agent, client_ip=client_ip,
    )
    if username is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def get_optional_admin(
    request: Request,
    session_token: str | None = Cookie(default=None),
) -> str | None:
    """FastAPI dependency — returns username or None (no error).

    ``request`` is required so fingerprint + client-IP binding can be
    checked. Callers that want to skip the Request dependency can use
    :func:`get_optional_admin_from_token` directly with a raw token.
    """
    return get_optional_admin_from_token(session_token, request=request)


def get_optional_admin_from_token(
    session_token: str | None,
    *,
    request: Request | None = None,
) -> str | None:
    """Non-FastAPI entrypoint for "validate a session, nullable".

    Used where the token is already pulled out of cookies manually
    (e.g. keep-page admin gating). Fingerprint binding is best-effort:
    if no request is supplied we skip the UA/IP check.
    """
    if not session_token:
        return None
    conn = get_db()
    if request is not None:
        user_agent = request.headers.get("user-agent", "")
        client_ip = get_client_ip(request)
    else:
        user_agent = ""
        client_ip = ""
    return validate_session(
        conn, session_token, user_agent=user_agent, client_ip=client_ip,
    )


def require_admin_or_redirect(request: Request):
    """For page routes — redirects to login instead of 401."""
    session_token = request.cookies.get("session_token")
    if not session_token:
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    user_agent = request.headers.get("user-agent", "")
    client_ip = get_client_ip(request)
    username = validate_session(
        conn, session_token, user_agent=user_agent, client_ip=client_ip,
    )
    if username is None:
        return RedirectResponse("/login", status_code=302)
    return username
