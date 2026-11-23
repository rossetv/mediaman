"""Recommended page (GET /recommended): a redirect to the Watch page, which shows the picks."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from mediaman.web.auth.middleware import resolve_page_session

router = APIRouter()


@router.get("/recommended")
def recommended_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    return RedirectResponse("/watch", status_code=302)
