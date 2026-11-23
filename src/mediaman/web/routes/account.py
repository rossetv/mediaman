"""Account page: who you are, plus a change-password form for any signed-in user."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.web.auth.middleware import resolve_page_session

router = APIRouter()


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, _conn = resolved

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "username": username,
            "role": getattr(request.state, "user_role", "admin"),
            "nav_active": "account",
        },
    )
