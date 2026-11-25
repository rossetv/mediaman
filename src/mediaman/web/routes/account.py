"""Profile page of any signed-in user: who they are, their email, password, sign-ins and Plex account."""

from __future__ import annotations

import sqlite3
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.web.auth._token_hashing import hash_token
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.auth.password_hash import get_user_email
from mediaman.web.auth.reauth import has_recent_reauth, reauth_window_seconds

router = APIRouter()


def _password_good_for(conn: sqlite3.Connection, token: str | None, username: str) -> int:
    """Seconds until this session is asked for its password again; a hint for the page only."""
    if not token or not has_recent_reauth(conn, token, username):
        return 0
    row = conn.execute(
        "SELECT granted_at, expires_at FROM reauth_tickets WHERE session_token_hash = ?",
        (hash_token(token),),
    ).fetchone()
    granted = parse_iso_strict_utc(row["granted_at"]) if row else None
    expires = parse_iso_strict_utc(row["expires_at"]) if row else None
    if granted is None or expires is None:
        return 0
    now = now_utc()
    left = min(
        (expires - now).total_seconds(),
        reauth_window_seconds() - (now - granted).total_seconds(),
    )
    return max(0, int(left))


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "username": username,
            "email": get_user_email(conn, username),
            "role": getattr(request.state, "user_role", "admin"),
            "nav_active": "account",
            "password_good_for": _password_good_for(
                conn, request.cookies.get("session_token"), username
            ),
        },
    )
