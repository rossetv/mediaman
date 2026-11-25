"""The page a TV's own browser opens, and the page where a signed-in person enters its code."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.db import get_db
from mediaman.services.infra import get_string_setting
from mediaman.web.auth.middleware import resolve_page_session

router = APIRouter()


def _base_url() -> str:
    """The Base URL setting, or "" where it is not the address of a page."""
    base = (get_string_setting(get_db(), "base_url") or "").strip().rstrip("/")
    try:
        parts = urlsplit(base)
    except ValueError:
        return ""
    return base if parts.scheme in ("http", "https") and parts.hostname else ""


@router.get("/tv", response_class=HTMLResponse)
def tv_page(request: Request) -> Response:
    """Signed out, whatever cookie comes with the request."""
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(request, "tv.html", {"base_url": _base_url()})


@router.get("/pair", response_class=HTMLResponse)
def pair_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, _conn = resolved
    templates = cast(Jinja2Templates, request.app.state.templates)
    tv_address = (_base_url() or str(request.base_url).rstrip("/")).split("://", 1)[1] + "/tv"
    return templates.TemplateResponse(
        request,
        "pair.html",
        {"username": username, "nav_active": "pair", "tv_address": tv_address},
    )
