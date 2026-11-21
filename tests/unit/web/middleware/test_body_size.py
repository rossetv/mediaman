"""Tests for BodySizeLimitMiddleware on streamed bodies with no Content-Length."""

from __future__ import annotations

import asyncio
import logging

import pytest
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mediaman.web.middleware.body_size import BodySizeLimitMiddleware
from mediaman.web.middleware.security_headers import SecurityHeadersMiddleware

_CAP = 100


async def _raw(request: Request) -> PlainTextResponse:
    return PlainTextResponse(str(len(await request.body())))


async def _form(request: Request) -> PlainTextResponse:
    form = await request.form()
    return PlainTextResponse(str(len(form)))


def _app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/raw", _raw, methods=["POST"]),
            Route("/form", _form, methods=["POST"]),
        ]
    )
    # Production order: security headers wrap the body-size cap.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=_CAP)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _post(
    path: str, chunks: list[bytes], content_type: str, *, disconnect_last: bool = False
) -> list[dict]:
    """Drive the app over raw ASGI; no Content-Length header is sent."""
    queue: list[dict] = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    if disconnect_last:
        queue[-1] = {"type": "http.disconnect"}
    sent: list[dict] = []

    async def receive() -> dict:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [(b"host", b"testserver"), (b"content-type", content_type.encode())],
    }
    asyncio.run(_app()(scope, receive, send))
    return sent


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/raw", "application/octet-stream"), ("/form", "application/x-www-form-urlencoded")],
)
def test_oversize_streamed_body_gets_413(path, content_type, caplog):
    chunks = [b"a=" + b"x" * 58, b"y" * 60]
    with caplog.at_level(logging.ERROR):
        sent = _post(path, chunks, content_type)

    start = sent[0]
    assert start["type"] == "http.response.start"
    assert start["status"] == 413
    assert b"x-content-type-options" in {k.lower() for k, _ in start["headers"]}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_streamed_body_under_cap_is_served():
    sent = _post("/raw", [b"x" * 40, b"y" * 40], "application/octet-stream")
    assert sent[0]["status"] == 200


def test_real_disconnect_under_cap_still_raises():
    with pytest.raises(ClientDisconnect):
        _post("/raw", [b"x" * 40, b"y" * 40], "application/octet-stream", disconnect_last=True)
