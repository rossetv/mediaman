"""Profile page of any signed-in user: who they are, their email, password, sign-ins and Plex account."""

from __future__ import annotations

import sqlite3
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.format import format_day_month
from mediaman.services.avatars import drawn
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.auth.password_hash import get_user_email
from mediaman.web.auth.password_hash._change_password import password_changed_at
from mediaman.web.auth.reauth import reauth_seconds_left
from mediaman.web.repository import avatars

router = APIRouter()

#: What each drawn avatar is called for those who cannot see it, in order.
_FACES = (
    "Curly dark hair, teal jumper",
    "White beard, round glasses",
    "Teal headscarf",
    "Dark beard, red jumper",
    "Silver hair, plum glasses",
    "Black hair, cream hoodie",
    "Long red hair",
    "Orange head wrap",
    "Short fair hair",
    "Grey curls, dark glasses",
)


def _picture(conn: sqlite3.Connection, username: str) -> dict[str, object]:
    user_id = avatars.find_user_id(conn, username) or 0
    avatar = avatars.get_avatar(conn, user_id)
    return {
        "url": avatar.url,
        "choice": avatar.choice,
        "photo": avatar.file_name is not None,
        "default": drawn.default_choice(user_id),
        "drawn": [(drawn.url(n), face) for n, face in enumerate(_FACES, 1)],
    }


def _password_changed(conn: sqlite3.Connection, username: str) -> dict[str, str] | None:
    at = password_changed_at(conn, username)
    if at is None:
        return None
    return {"at": at.isoformat(), "text": format_day_month(at, long_month=True)}


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
            "password_changed": _password_changed(conn, username),
            "picture": _picture(conn, username),
            "role": getattr(request.state, "user_role", "admin"),
            "nav_active": "account",
            "password_good_for": reauth_seconds_left(
                conn, request.cookies.get("session_token"), username
            ),
        },
    )
