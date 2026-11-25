"""Styled 404, 500 and refused-request pages for browsers; JSON and plain text stay for the rest.

A page is sent only when the request asks for HTML and is not under ``/api/``. Every other
caller keeps the answer it had: the framework's JSON for an HTTP error, plain text for a
crash. The page names no path, exception or setting.
"""

from __future__ import annotations

import json
from typing import cast

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp

from mediaman.web.middleware.security_headers import SecurityHeadersMiddleware
from mediaman.web.responses import respond_failure

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


async def _validation_error(request: Request, exc: Exception) -> Response:
    """The framework's 422, without the value that was sent: it may be a password."""
    assert isinstance(exc, RequestValidationError)
    errors = [{k: v for k, v in error.items() if k != "input"} for error in exc.errors()]
    # Escaped to ASCII, since the names of the fields are the caller's own text.
    body = json.dumps({"detail": jsonable_encoder(errors)})
    return Response(body, status_code=422, media_type="application/json")


async def _with_security_headers(request: Request, response: Response) -> Response:
    """*response* with the headers the security middleware puts on every other response.

    The crash handler runs outside that middleware, so the same code is run over its answer.
    """

    async def answer(_request: Request) -> Response:
        return response

    # The middleware never touches its wrapped app when dispatched directly.
    return await SecurityHeadersMiddleware(cast(ASGIApp, None)).dispatch(request, answer)


async def _server_error(request: Request, exc: Exception) -> Response:
    if _wants_html(request.url.path, request.headers.get("accept", "")):
        page = _render(request, 500)
        if page is not None:
            return await _with_security_headers(request, page)
    return await _with_security_headers(
        request, PlainTextResponse("Internal Server Error", status_code=500)
    )


def refusal(request: Request, body: bytes) -> Response:
    """A 403 for a refused request: the error page for a browser navigation, else *body* as is."""
    if _wants_html(request.url.path, request.headers.get("accept", "")):
        page = _render(request, 403)
        if page is not None:
            return page
    return Response(status_code=403, content=body)


_UNUSABLE_TEXT = "The request holds a character that cannot be used."


class _UnusableText(Exception):
    """A JSON body holds text with no UTF-8 form."""


async def _unusable_text(request: Request, exc: Exception) -> Response:
    """The app's usual failure, so its pages show the words; ``detail`` for those that read it."""
    return respond_failure(_UNUSABLE_TEXT, status=400, detail=_UNUSABLE_TEXT)


async def refuse_text_with_no_utf8_form(request: Request) -> None:
    """Refuse with a 400 a JSON body holding text that has no UTF-8 form, as half of a
    surrogate pair has not.

    JSON's escapes can spell such text, and it would fail wherever it is stored or sent on, so
    it is turned away where it enters; the server's own text is left to fail loudly.
    """
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json" and not kind.endswith("+json"):
        return
    try:
        body = await request.json()
    except ValueError:
        return  # Not JSON at all: the route refuses it as it would any other.
    try:
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise _UnusableText from None


def register_error_pages(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(_UnusableText, _unusable_text)
    app.add_exception_handler(Exception, _server_error)
