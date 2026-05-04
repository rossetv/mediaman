"""Retry / backoff orchestration for outbound HTTP calls.

Responsibility
--------------
Wrap a single-attempt HTTP dispatch function in a retry loop that handles
two classes of transient failure:

* **Transport errors** — :class:`requests.Timeout`, :class:`requests.ConnectionError`,
  and a handful of related exceptions that indicate the remote end went away
  or the connection was reset.  These are retried with a fixed backoff
  schedule on any method when the caller opts in.

* **Retryable status codes** — 429, 502, 503, 504.  On 429/503 we honour a
  server-supplied ``Retry-After`` header (both delta-seconds and HTTP-date
  forms) capped to :data:`_RETRY_AFTER_MAX_SECONDS` so a pathological
  upstream cannot pin a worker indefinitely.

Invariants
----------
- POST/PUT/DELETE never retry unless the caller explicitly passes
  ``retry=True``.  GET retries by default.  The retry flag is set in
  :class:`~mediaman.services.infra.http.client.SafeHTTPClient`, not here.
- The retry schedule is fixed at two extra attempts (three total) — the
  :data:`_RETRY_BACKOFFS` tuple drives the sleep between attempts.
- ``_dispatch_loop`` is pure — it calls the supplied *dispatch_fn* and
  *read_fn* and does not touch the session or SSRF state directly.
"""

from __future__ import annotations

import email.utils
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from mediaman.services.infra.http.streaming import (
    _ContentTypeMismatch,
    _SizeCapExceeded,
)

logger = logging.getLogger("mediaman")

# ---------------------------------------------------------------------------
# Retry constants
# ---------------------------------------------------------------------------

#: HTTP status codes treated as transient on idempotent requests.
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

#: Statuses for which a server-supplied ``Retry-After`` header is honoured.
_RETRY_AFTER_STATUSES = frozenset({429, 503})

#: Sleep durations between consecutive attempts (seconds).
_RETRY_BACKOFFS: tuple[float, ...] = (0.5, 1.0)

#: Hard cap on a server-supplied ``Retry-After`` delay.
_RETRY_AFTER_MAX_SECONDS = 60.0

#: Transport-layer exceptions that trigger the retry loop.
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.SSLError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    requests.exceptions.TooManyRedirects,
)

#: Bytes of the response body to include in a ``SafeHTTPError`` for debugging.
_BODY_SNIPPET_BYTES = 512


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into a delay in seconds.

    Accepts both the integer-seconds form (``"30"``) and the HTTP-date form
    (RFC 7231 §7.1.1.1).  Returns the delay capped to
    :data:`_RETRY_AFTER_MAX_SECONDS`.  Returns ``None`` for missing or
    unparseable values.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    # Integer-seconds form first — it's the common case.
    try:
        seconds = float(raw)
        if seconds < 0:
            return 0.0
        return min(seconds, _RETRY_AFTER_MAX_SECONDS)
    except (TypeError, ValueError):
        pass
    # HTTP-date form.
    try:
        target = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    # ``parsedate_to_datetime`` returns naive on missing offsets — normalise.
    now = datetime.now(UTC)
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    delta = (target - now).total_seconds()
    if delta <= 0:
        return 0.0
    return min(delta, _RETRY_AFTER_MAX_SECONDS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snippet(body: bytes) -> str:
    """Return a short UTF-8 snippet of *body* suitable for error messages."""
    if not body:
        return ""
    return body[:_BODY_SNIPPET_BYTES].decode("utf-8", errors="replace")


def _safe_path(url: str) -> str:
    """Return ``host/path`` of *url* for log messages, never the query string.

    Query strings on outbound URLs often contain API keys — never log them.
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or "?"
        return f"{host}{parsed.path or '/'}"
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# Dispatch loop
# ---------------------------------------------------------------------------


def dispatch_loop(
    *,
    dispatch_fn: Callable[[], requests.Response],
    read_fn: Callable[[requests.Response], bytes],
    method: str,
    url: str,
    attempts: int,
    make_error: Callable[..., Exception],
) -> requests.Response:
    """Inner dispatch + retry loop.

    Args:
        dispatch_fn: Zero-arg callable that issues a single HTTP request and
            returns a :class:`requests.Response` (streaming mode).
        read_fn: Callable that reads the capped body from the response,
            raising :class:`_SizeCapExceeded` or :class:`_ContentTypeMismatch`
            on violations.
        method: HTTP verb for log messages.
        url: Full URL for log messages and error attribution.
        attempts: Total number of attempts (``1 + len(_RETRY_BACKOFFS)`` when
            retrying, ``1`` otherwise).
        make_error: Factory that produces a ``SafeHTTPError``-style exception
            given ``(status_code, body_snippet, url)`` keyword args.  Kept as
            a callable to avoid a circular import with :mod:`.client`.
    """
    last_exc: Exception | None = None
    last_status: int | None = None
    last_snippet: str = ""

    for attempt in range(attempts):
        try:
            response = dispatch_fn()
        except _RETRYABLE_EXCEPTIONS as exc:
            logger.warning(
                "HTTP %s %s transport error: %s",
                method,
                _safe_path(url),
                type(exc).__name__,
            )
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(_RETRY_BACKOFFS[attempt])
                continue
            raise make_error(
                status_code=0,
                body_snippet=f"transport error: {type(exc).__name__}",
                url=url,
            ) from exc

        try:
            body = read_fn(response)
        except _SizeCapExceeded as exc:
            response.close()
            raise make_error(
                status_code=response.status_code,
                body_snippet=str(exc),
                url=url,
            ) from None
        except _ContentTypeMismatch as exc:
            response.close()
            raise make_error(
                status_code=response.status_code,
                body_snippet=str(exc),
                url=url,
            ) from None

        # Re-attach the buffered body so the caller can use .json(), .text, etc.
        response._content = body
        response._content_consumed = True  # type: ignore[attr-defined]

        if response.status_code in _RETRYABLE_STATUSES and attempt + 1 < attempts:
            logger.warning(
                "HTTP %s %s returned %s — retrying",
                method,
                _safe_path(url),
                response.status_code,
            )
            response.close()
            # Honour ``Retry-After`` on 429/503; fall back to fixed schedule.
            delay = _RETRY_BACKOFFS[attempt]
            if response.status_code in _RETRY_AFTER_STATUSES:
                advised = _retry_after_seconds(response.headers.get("Retry-After"))
                if advised is not None:
                    delay = advised
            time.sleep(delay)
            last_status = response.status_code
            last_snippet = _snippet(body)
            continue

        if not (200 <= response.status_code < 300):
            snippet = _snippet(body)
            response.close()
            raise make_error(
                status_code=response.status_code,
                body_snippet=snippet,
                url=url,
            )

        return response

    # All retries exhausted on a retryable status.
    if last_status is not None:
        raise make_error(
            status_code=last_status,
            body_snippet=last_snippet,
            url=url,
        )
    # Should be unreachable; keeps mypy happy.
    assert last_exc is not None
    raise last_exc


# Re-export for callers that import constants directly.
__all__ = [
    "_RETRYABLE_EXCEPTIONS",
    "_RETRYABLE_STATUSES",
    "_RETRY_AFTER_MAX_SECONDS",
    "_RETRY_AFTER_STATUSES",
    "_RETRY_BACKOFFS",
    "_retry_after_seconds",
    "dispatch_loop",
]
