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
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Literal
from urllib.parse import urlparse

import requests

from mediaman.core.time import now_utc
from mediaman.services.infra.http.streaming import (
    _ContentTypeMismatch,
    _SizeCapExceeded,
)

logger = logging.getLogger(__name__)

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
        pass  # not a delta-seconds value — fall through to the HTTP-date form
    # HTTP-date form.
    try:
        target = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # ``parsedate_to_datetime`` returns naive on missing offsets — normalise.
    now = now_utc()
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
        parsed = urlparse(url)
        host = parsed.hostname or "?"
        return f"{host}{parsed.path or '/'}"
    except ValueError:
        return "?"


# ---------------------------------------------------------------------------
# Dispatch loop
# ---------------------------------------------------------------------------


def _compute_delay(
    attempt: int,
    *,
    jitter_strategy: Literal["fixed", "full"],
) -> float:
    """Return the sleep delay for *attempt* (0-indexed) under the chosen jitter strategy.

    ``"fixed"`` indexes into :data:`_RETRY_BACKOFFS` (0.5s, 1.0s) — the
    schedule used by every mediaman outbound caller historically.
    ``"full"`` returns :func:`random.uniform(0, 2**attempt)` — full-jitter
    exponential backoff used by the mailgun POST path so a thundering
    herd of failed sends doesn't synchronise on the retry window.  This
    matches the pre-consolidation mailgun formula exactly: both
    ``_retry_with_jitter`` (now removed) and this helper iterate
    ``attempt`` from ``0`` upwards via ``range(attempts)``, so the
    expected sleep is the same — ``E[uniform(0, 2**attempt)] = 2**(attempt-1)``
    seconds, starting at 0.5s for the first retry.
    """
    if jitter_strategy == "full":
        return random.uniform(0, 2**attempt)
    if attempt < len(_RETRY_BACKOFFS):
        return _RETRY_BACKOFFS[attempt]
    return _RETRY_BACKOFFS[-1]


@dataclass
class _LoopState:
    """Carry-over state mutated across :func:`dispatch_loop` iterations.

    These four fields are the only state that survives between attempts and
    therefore must be threaded through the per-step helpers verbatim:

    * ``last_exc`` — the most recent transport exception, re-raised if every
      attempt failed at the transport layer.
    * ``last_status`` / ``last_snippet`` — the status code and body snippet of
      the most recent *retryable* response, used to build the final error
      once the attempt budget is exhausted on a retryable status.
    * ``consecutive_5xx`` — running count of back-to-back 5xx responses,
      driving the ``abort_after_consecutive_5xx`` early-abort policy. A
      transport error resets it to ``0``; a non-5xx retryable status (429)
      also resets it; a 5xx increments it.
    """

    last_exc: Exception | None = None
    last_status: int | None = None
    last_snippet: str = ""
    consecutive_5xx: int = 0


def _handle_transport_error(
    exc: Exception,
    *,
    method: str,
    url: str,
    attempt: int,
    attempts: int,
    jitter_strategy: Literal["fixed", "full"],
    make_error: Callable[..., Exception],
    state: _LoopState,
) -> None:
    """Handle a transport-layer exception raised by ``dispatch_fn``.

    Records the exception on *state*, resets the consecutive-5xx streak (a
    transport error breaks any 5xx run), then either sleeps and returns — so
    the caller's loop ``continue``s to the next attempt — or, when the attempt
    budget is exhausted, raises the ``make_error`` exception. The behaviour is
    identical to the inline ``except _RETRYABLE_EXCEPTIONS`` block it replaces.
    """
    logger.warning(
        "HTTP %s %s transport error: %s",
        method,
        _safe_path(url),
        type(exc).__name__,
    )
    state.last_exc = exc
    # Transport errors break any consecutive-5xx streak.
    state.consecutive_5xx = 0
    if attempt + 1 < attempts:
        time.sleep(_compute_delay(attempt, jitter_strategy=jitter_strategy))
        return
    raise make_error(
        status_code=0,
        body_snippet=f"transport error: {type(exc).__name__}",
        url=url,
    ) from exc


def _handle_retryable_status(
    response: requests.Response,
    body: bytes,
    *,
    method: str,
    url: str,
    attempt: int,
    attempts: int,
    jitter_strategy: Literal["fixed", "full"],
    abort_after_consecutive_5xx: int | None,
    make_error: Callable[..., Exception],
    state: _LoopState,
) -> None:
    """Handle a retryable status code (429/5xx) when more attempts remain.

    Maintains the ``consecutive_5xx`` counter on *state*, enforces the
    ``abort_after_consecutive_5xx`` early-abort policy (raising ``make_error``
    when the threshold is hit), honours a ``Retry-After`` header on 429/503,
    sleeps for the resulting delay, and finally records ``last_status`` /
    ``last_snippet`` on *state* so the post-loop branch can build the final
    error. On return the caller's loop ``continue``s to the next attempt. The
    body is logic-identical to the inline retryable-status block it replaces.

    Only called when ``response.status_code`` is in the retryable set *and*
    ``attempt + 1 < attempts``.
    """
    logger.warning(
        "HTTP %s %s returned %s — retrying",
        method,
        _safe_path(url),
        response.status_code,
    )
    # Maintain the consecutive-5xx counter for early-abort callers.
    if 500 <= response.status_code < 600:
        state.consecutive_5xx += 1
    else:
        state.consecutive_5xx = 0
    if (
        abort_after_consecutive_5xx is not None
        and state.consecutive_5xx >= abort_after_consecutive_5xx
    ):
        logger.warning(
            "HTTP %s %s: %d consecutive 5xx — aborting retries",
            method,
            _safe_path(url),
            state.consecutive_5xx,
        )
        snippet = _snippet(body)
        response.close()
        raise make_error(
            status_code=response.status_code,
            body_snippet=snippet,
            url=url,
        )
    response.close()
    # Honour ``Retry-After`` on 429/503; fall back to the schedule.
    delay = _compute_delay(attempt, jitter_strategy=jitter_strategy)
    if response.status_code in _RETRY_AFTER_STATUSES:
        advised = _retry_after_seconds(response.headers.get("Retry-After"))
        if advised is not None:
            delay = advised
    time.sleep(delay)
    state.last_status = response.status_code
    state.last_snippet = _snippet(body)


def _finalise_response(
    response: requests.Response,
    body: bytes,
    *,
    url: str,
    make_error: Callable[..., Exception],
) -> requests.Response:
    """Return *response* on a 2xx status, or raise ``make_error`` otherwise.

    Reached once a response is neither a transport error nor a retryable
    status with attempts left (either it is a 2xx, a non-retryable status, or
    a retryable status on the final attempt). Behaviour is identical to the
    inline non-2xx raise / ``return response`` it replaces.
    """
    if not (200 <= response.status_code < 300):
        snippet = _snippet(body)
        response.close()
        raise make_error(
            status_code=response.status_code,
            body_snippet=snippet,
            url=url,
        )
    return response


def dispatch_loop(
    *,
    dispatch_fn: Callable[[], requests.Response],
    read_fn: Callable[[requests.Response], bytes],
    method: str,
    url: str,
    attempts: int,
    make_error: Callable[..., Exception],
    jitter_strategy: Literal["fixed", "full"] = "fixed",
    abort_after_consecutive_5xx: int | None = None,
    retryable_statuses: frozenset[int] | None = None,
) -> requests.Response:
    """Issue an HTTP request with transient-failure retry and backoff.

    Issues a single HTTP request and retries on transient failures: transport errors
    (timeout, connection reset, SSL errors), and HTTP status codes 429, 502, 503, 504.
    Retry policy: up to ``attempts`` total attempts, with backoff between attempts
    determined by *jitter_strategy*.  Honours ``Retry-After`` on 429/503 responses
    (delta-seconds and HTTP-date forms, capped to 60 seconds).

    The loop body reads as three named steps — transport-error handling
    (:func:`_handle_transport_error`), retryable-status handling
    (:func:`_handle_retryable_status`), and the final non-2xx raise / success
    return (:func:`_finalise_response`) — with the cross-iteration carry-over
    state held in a single :class:`_LoopState`.

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
        jitter_strategy: ``"fixed"`` (default) uses :data:`_RETRY_BACKOFFS`;
            ``"full"`` uses full-jitter exponential backoff
            ``uniform(0, 2**attempt)`` for callers (e.g. mailgun) that want a
            wider sleep envelope so retries don't synchronise.
        abort_after_consecutive_5xx: When set, abort the retry loop early
            after this many consecutive 5xx responses — the mailgun policy
            uses ``2`` to give up when the remote is clearly degraded.
            ``None`` (default) keeps the historical behaviour of running the
            full attempt budget.
        retryable_statuses: Override :data:`_RETRYABLE_STATUSES` for callers
            with a different transient-set (mailgun adds ``500``).  ``None``
            uses the default set.

    Returns:
        The final :class:`requests.Response` on success (2xx status code).

    Raises:
        Exception: Produced by *make_error* on non-2xx final status or transport error.
    """
    retryable = retryable_statuses or _RETRYABLE_STATUSES
    state = _LoopState()

    for attempt in range(attempts):
        try:
            response = dispatch_fn()
        except _RETRYABLE_EXCEPTIONS as exc:
            _handle_transport_error(
                exc,
                method=method,
                url=url,
                attempt=attempt,
                attempts=attempts,
                jitter_strategy=jitter_strategy,
                make_error=make_error,
                state=state,
            )
            continue

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

        if response.status_code in retryable and attempt + 1 < attempts:
            _handle_retryable_status(
                response,
                body,
                method=method,
                url=url,
                attempt=attempt,
                attempts=attempts,
                jitter_strategy=jitter_strategy,
                abort_after_consecutive_5xx=abort_after_consecutive_5xx,
                make_error=make_error,
                state=state,
            )
            continue

        return _finalise_response(response, body, url=url, make_error=make_error)

    # All retries exhausted on a retryable status.
    if state.last_status is not None:
        raise make_error(
            status_code=state.last_status,
            body_snippet=state.last_snippet,
            url=url,
        )
    # Should be unreachable; keeps mypy happy.
    assert state.last_exc is not None
    raise state.last_exc


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
