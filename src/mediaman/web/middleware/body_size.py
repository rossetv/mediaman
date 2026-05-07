"""Pure-ASGI body-size cap to short-circuit oversize uploads as bytes
stream in.

Mediaman is single-process and single-worker; a multi-gigabyte POST
trivially exhausts memory and stalls the event loop.  This middleware
rejects requests whose declared ``Content-Length`` exceeds the cap
*before* reading the body, and counts streaming bytes for chunked
requests so it can short-circuit mid-flight.
"""

from __future__ import annotations

import logging
import os

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


# Default cap: 8 MiB. Mediaman is single-process and single-worker; a
# multi-gigabyte POST trivially exhausts memory and stalls the event
# loop.  Operators who upload large files (e.g. avatars, imports)
# can raise the cap via ``MEDIAMAN_MAX_REQUEST_BYTES``.
_DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024


def _resolve_max_request_bytes() -> int:
    """Return the configured per-request body-size cap in bytes.

    Reads ``MEDIAMAN_MAX_REQUEST_BYTES`` from the environment.  Falls
    back to :data:`_DEFAULT_MAX_REQUEST_BYTES` when unset, blank, or
    unparseable; logs a warning if the value parses but is negative
    (which would silently disable the limit).
    """
    raw = (os.environ.get("MEDIAMAN_MAX_REQUEST_BYTES") or "").strip()
    if not raw:
        return _DEFAULT_MAX_REQUEST_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "MEDIAMAN_MAX_REQUEST_BYTES=%r is not an integer; using default of %d bytes",
            raw,
            _DEFAULT_MAX_REQUEST_BYTES,
        )
        return _DEFAULT_MAX_REQUEST_BYTES
    if value < 0:
        logger.warning(
            "MEDIAMAN_MAX_REQUEST_BYTES=%d is negative; using default of %d bytes",
            value,
            _DEFAULT_MAX_REQUEST_BYTES,
        )
        return _DEFAULT_MAX_REQUEST_BYTES
    return value


async def _send_413(send: Send) -> None:
    """Emit a minimal ``413 Payload Too Large`` response with ``close``."""
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"connection", b"close"),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b"Payload too large",
            "more_body": False,
        }
    )


class BodySizeLimitMiddleware:
    """Reject requests whose body exceeds the configured cap.

    Pure ASGI middleware (not :class:`BaseHTTPMiddleware`) — Starlette's
    BaseHTTPMiddleware buffers the entire body before invoking the
    handler, so it cannot enforce a cap *as bytes stream in*.  By
    consuming the ``http.request`` chunks directly we can short-circuit
    the moment the running total exceeds the limit, before any handler
    has a chance to allocate.

    Two checks:

    1. **Declared length** — if a ``Content-Length`` header is present
       and already exceeds the cap, reject before reading any body.
    2. **Actual length** — if the request is chunked (no
       ``Content-Length``) or the header is wrong, count bytes as they
       arrive in ``http.request`` chunks and reject mid-stream.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        # ``max_bytes=None`` means "read the env var on first request"
        # so test harnesses that monkeypatch the env after construction
        # see the new value.  Cache after the first lookup so we don't
        # re-parse on every request.
        self._configured_max = max_bytes
        self._cached_max: int | None = None

    def _max_bytes(self) -> int:
        if self._configured_max is not None:
            return self._configured_max
        if self._cached_max is None:
            self._cached_max = _resolve_max_request_bytes()
        return self._cached_max

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = self._max_bytes()
        # ``max_bytes == 0`` would block every request — treat as
        # "unlimited" instead, matching the spirit of "operator opt-out".
        if max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        # Fast path: declared Content-Length over the cap.
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"content-length":
                try:
                    declared = int(header_value)
                except ValueError:
                    declared = -1
                if declared > max_bytes:
                    await _send_413(send)
                    return
                break

        # Read the first request message up front so we can short-
        # circuit with a 413 without invoking the inner app at all when
        # the body already exceeds the cap.  Subsequent chunks are
        # checked as they arrive in the streaming wrapper below.
        first_message = await receive()
        if first_message["type"] != "http.request":
            # Disconnect (or other non-request first frame) — pass
            # through as-is so Starlette can drain cleanly.
            async def _passthrough_first() -> Message:
                return first_message

            await self.app(scope, _passthrough_first, send)
            return

        bytes_received = len(first_message.get("body", b"") or b"")
        if bytes_received > max_bytes:
            await _send_413(send)
            return

        # If the first frame already carried the entire body, replay it
        # straight through.  Otherwise wrap ``receive`` so subsequent
        # chunks are size-checked as they stream in.
        more_body = bool(first_message.get("more_body", False))
        first_consumed = False
        body_total = bytes_received
        oversize = False

        async def _streaming_receive() -> Message:
            nonlocal first_consumed, body_total, oversize
            if not first_consumed:
                first_consumed = True
                return first_message
            if oversize:
                # Once we've sent a 413 we tell the inner app the
                # client disconnected; it should give up rather than
                # block forever waiting for more bytes.
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"") or b""
                body_total += len(chunk)
                if body_total > max_bytes:
                    oversize = True
                    return {"type": "http.disconnect"}
            return message

        # Track whether the inner app started a response so we know
        # whether emitting a 413 is still safe.  ASGI forbids two
        # ``http.response.start`` frames on the same scope.
        response_started = False

        async def _watching_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        # If a body limit has already been breached on the first chunk,
        # we returned the 413 above.  Otherwise let the inner app run
        # against the streaming wrapper.  The inner app may start
        # writing headers before all body bytes arrive (e.g. uploads
        # streamed straight to disk); if a later chunk pushes the body
        # over the cap, we stop forwarding bytes but cannot replace the
        # already-started response.  In that case the connection will
        # be closed by the wrapper returning ``http.disconnect``.
        if not more_body:
            await self.app(scope, _streaming_receive, _watching_send)
            return

        await self.app(scope, _streaming_receive, _watching_send)
        if oversize and not response_started:
            # Edge case: handler returned without starting a response
            # (e.g. it short-circuited on the disconnect we forced).
            # Emit a clean 413 in that case.
            await _send_413(send)
