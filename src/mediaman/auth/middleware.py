"""FastAPI auth dependencies."""

from fastapi import Cookie, HTTPException, Request
from starlette.responses import RedirectResponse

from mediaman.db import get_db
from mediaman.auth.session import validate_session


def get_current_admin(session_token: str | None = Cookie(default=None)) -> str:
    """FastAPI dependency — returns username or raises 401."""
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    username = validate_session(conn, session_token)
    if username is None:
        raise HTTPException(status_code=401, detail="Session expired")
    return username


def get_optional_admin(session_token: str | None = Cookie(default=None)) -> str | None:
    """FastAPI dependency — returns username or None (no error)."""
    if not session_token:
        return None
    conn = get_db()
    return validate_session(conn, session_token)


def require_admin_or_redirect(request: Request):
    """For page routes — redirects to login instead of 401."""
    session_token = request.cookies.get("session_token")
    if not session_token:
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    username = validate_session(conn, session_token)
    if username is None:
        return RedirectResponse("/login", status_code=302)
    return username
