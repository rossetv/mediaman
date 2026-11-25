"""Profile page of any signed-in user: who they are, their email, password, sign-ins and Plex account."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.auth.password_hash import get_user_email
from mediaman.web.auth.reauth import reauth_seconds_left

router = APIRouter()


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
            "password_good_for": reauth_seconds_left(
                conn, request.cookies.get("session_token"), username
            ),
        },
    )
