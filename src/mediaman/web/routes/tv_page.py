"""The page a TV's own browser opens, and the page where a signed-in person enters its code."""

from __future__ import annotations

import ipaddress
import re
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


# Names that mean something in one network alone.
_INTERNAL_NAME_RE = re.compile(
    r"[^.]+|.+\.(?:local|localdomain|localhost|lan|home|home\.arpa|internal|intranet|corp|private)"
)


def _base_url() -> str:
    """The address in the Base URL setting, or "" where it is not that of a page.

    Nothing else the setting may hold comes with it, such as a name and password.
    """
    try:
        parts = urlsplit((get_string_setting(get_db(), "base_url") or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}{parts.path}".rstrip("/")


def _public_base_url() -> str:
    """The same, or "" where only the network the server stands in knows it."""
    base = _base_url()
    host = (urlsplit(base).hostname or "").rstrip(".")
    try:
        internal = not ipaddress.ip_address(host).is_global
    except ValueError:
        internal = _INTERNAL_NAME_RE.fullmatch(host) is not None
    return "" if internal else base


@router.get("/tv", response_class=HTMLResponse)
def tv_page(request: Request) -> Response:
    """Signed out, whatever cookie comes with the request.

    It shows the address to pair at. With none to show, the page takes the one it was opened at.
    """
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(request, "tv.html", {"base_url": _public_base_url()})


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
