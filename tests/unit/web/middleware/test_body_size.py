"""Tests for BodySizeLimitMiddleware on streamed bodies with no Content-Length."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import pytest
from fastapi import Body, FastAPI, Form
from starlette.requests import ClientDisconnect, Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mediaman.web.middleware.body_size import BodySizeLimitMiddleware
from mediaman.web.middleware.security_headers import SecurityHeadersMiddleware

_CAP = 100


async def _raw(request: Request) -> PlainTextResponse:
    return PlainTextResponse(str(len(await request.body())))


async def _form(request: Request) -> PlainTextResponse:
    form = await request.form()
    return PlainTextResponse(str(len(form)))


def _app(*, bare: bool = False) -> ASGIApp:
    app = FastAPI(
        routes=[
            Route("/raw", _raw, methods=["POST"]),
            Route("/form", _form, methods=["POST"]),
        ]
    )

    # Typed parameters make FastAPI read the body itself and turn a read
    # failure into its own 400.
    @app.post("/typed-form")
    def typed_form(duration: str = Form(...)) -> dict:
        return {"duration": duration}

    @app.post("/typed-body")
    def typed_body(payload: Annotated[dict, Body()]) -> dict:
        return payload

    if bare:
        return BodySizeLimitMiddleware(app, max_bytes=_CAP)
    # Production order: security headers wrap the body-size cap.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=_CAP)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _post(
    path: str,
    chunks: list[bytes],
    content_type: str,
    *,
    disconnect_last: bool = False,
    app: ASGIApp | None = None,
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
    asyncio.run((app or _app())(scope, receive, send))
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


@pytest.mark.parametrize(
    ("path", "content_type", "chunks"),
    [
        ("/typed-form", "application/x-www-form-urlencoded", [b"duration=" + b"x" * 60, b"y" * 60]),
        ("/typed-body", "application/json", [b'{"a":"' + b"x" * 60, b"y" * 60 + b'"}']),
    ],
)
def test_oversize_streamed_body_on_typed_route_gets_one_413(path, content_type, chunks, caplog):
    with caplog.at_level(logging.ERROR):
        sent = _post(path, chunks, content_type)

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert [m["status"] for m in starts] == [413]
    assert b"x-content-type-options" in {k.lower() for k, _ in starts[0]["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert body == b"Payload too large"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    ("path", "content_type", "chunks"),
    [
        ("/typed-form", "application/x-www-form-urlencoded", [b"duration=" + b"x" * 60, b"y" * 60]),
        ("/typed-body", "application/json", [b'{"a":"' + b"x" * 60, b"y" * 60 + b'"}']),
    ],
)
def test_typed_route_oversize_sends_a_single_bare_413(path, content_type, chunks):
    sent = _post(path, chunks, content_type, app=_app(bare=True))

    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert body == b"Payload too large"


def test_response_started_before_the_cap_is_passed_is_not_replaced():
    async def early(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"one,", "more_body": True})
        while (await receive())["type"] == "http.request":
            pass
        await send({"type": "http.response.body", "body": b"two", "more_body": False})

    app = BodySizeLimitMiddleware(early, max_bytes=_CAP)
    sent = _post("/early", [b"x" * 60, b"y" * 60], "application/octet-stream", app=app)

    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [200]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert body == b"one,two"


def test_streamed_body_under_cap_is_served():
    sent = _post("/raw", [b"x" * 40, b"y" * 40], "application/octet-stream")
    assert sent[0]["status"] == 200


def test_real_disconnect_under_cap_still_raises():
    with pytest.raises(ClientDisconnect):
        _post("/raw", [b"x" * 40, b"y" * 40], "application/octet-stream", disconnect_last=True)
