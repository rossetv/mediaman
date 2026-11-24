"""Styled 404, 500 and refused-request pages for browsers; JSON and plain text stay for the rest.

A page is sent only when the request asks for HTML and is not under ``/api/``. Every other
caller keeps the answer it had: the framework's JSON for an HTTP error, plain text for a
crash. The page names no path, exception or setting.
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

_MESSAGES = {
    403: ("Request blocked", "That request did not come from this site, so it was refused."),
    404: ("Page not found", "That page does not exist, or it has moved."),
    500: ("Something went wrong", "Mediaman hit a problem loading this page. Try again shortly."),
}


def _wants_html(path: str, accept: str) -> bool:
    return not path.startswith("/api/") and "text/html" in accept.lower()


def _render(request: Request, status: int) -> Response | None:
    """The error page for *status*, or None when it cannot be drawn."""
    title, message = _MESSAGES[status]
    try:
        templates = cast(Jinja2Templates, request.app.state.templates)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status": status, "title": title, "message": message},
            status_code=status,
        )
    except Exception:
        # rationale: the page is a courtesy; a template fault must not hide the real error.
        return None


async def _http_exception(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404 and _wants_html(request.url.path, request.headers.get("accept", "")):
        page = _render(request, 404)
        if page is not None:
            return page
    return await http_exception_handler(request, exc)


async def _server_error(request: Request, exc: Exception) -> Response:
    if _wants_html(request.url.path, request.headers.get("accept", "")):
        page = _render(request, 500)
        if page is not None:
            return page
    return PlainTextResponse("Internal Server Error", status_code=500)


def refusal(request: Request, body: bytes) -> Response:
    """A 403 for a refused request: the error page for a browser navigation, else *body* as is."""
    if _wants_html(request.url.path, request.headers.get("accept", "")):
        page = _render(request, 403)
        if page is not None:
            return page
    return Response(status_code=403, content=body)


def register_error_pages(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(Exception, _server_error)
