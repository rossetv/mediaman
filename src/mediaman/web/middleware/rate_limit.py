"""``@rate_limit`` decorator for FastAPI route handlers.

Moved from ``mediaman.services.rate_limit.decorator`` because the decorator
is inherently FastAPI-coupled: it extracts the ``Request`` object from the
handler's arguments and calls ``mediaman.web.responses.respond_err`` to
return a 429 JSON response.  Placing it in ``services/`` violated the
rule that services may not import from ``web/``.

The non-FastAPI building blocks (``RateLimiter``, ``ActionRateLimiter``,
``get_client_ip``) remain in :mod:`mediaman.services.rate_limit` where they
can be used without any web-layer dependency.

Wraps an endpoint function so that a limiter check runs before the
handler body, returning a 429 response immediately when throttled.  This
eliminates the repetitive boilerplate:

    if not _SOME_LIMITER.check(actor):
        logger.warning(...)
        return respond_err("too_many_requests", status=429, ...)

Usage
-----
Actor key (authenticated admin routes)::

    @router.post("/api/something")
    @rate_limit(_MY_LIMITER, key="actor")
    def my_handler(request: Request, admin: str = Depends(get_current_admin)) -> Response:
        ...

IP key (unauthenticated or public routes)::

    @router.get("/public/thing")
    @rate_limit(_MY_LIMITER, key="ip")
    def public_handler(request: Request) -> Response:
        ...

Both cases expect the wrapped function to receive a ``request: Request``
positional-or-keyword argument so client-IP resolution works without
re-importing request from elsewhere.

Logging
-------
Throttle events are logged at WARNING level as::

    rate_limit.throttled scope=<key> actor=<actor_or_ip>

This replaces the ad-hoc per-site log lines
(e.g. ``"user.create_throttled actor=%s"``) with a single canonical
shape that log aggregators can filter on uniformly.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from fastapi import Request

from mediaman.services.rate_limit.ip_resolver import get_client_ip
from mediaman.services.rate_limit.limiters import ActionRateLimiter, RateLimiter
from mediaman.web.responses import respond_err

_logger = logging.getLogger(__name__)

# Union type accepted as the limiter argument.
_AnyLimiter = ActionRateLimiter | RateLimiter


def rate_limit(
    limiter: _AnyLimiter,
    *,
    key: str = "actor",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a FastAPI route handler with an inline rate-limit check.

    Args:
        limiter: A :class:`~mediaman.services.rate_limit.limiters.RateLimiter`
                 or :class:`~mediaman.services.rate_limit.limiters.ActionRateLimiter`
                 instance to check before calling the handler.
        key:     ``"actor"`` (default) — keyed on the ``admin`` parameter
                 resolved by ``Depends(get_current_admin)``.
                 ``"ip"`` — keyed on the client IP from the ``request``
                 parameter.

    Returns:
        A decorator that wraps the route function.

    Raises:
        ValueError: At decoration time if *key* is not ``"actor"`` or
                    ``"ip"``, or if the required parameter is absent from
                    the function signature.
    """
    if key not in ("actor", "ip"):
        raise ValueError(f"rate_limit key must be 'actor' or 'ip', got {key!r}")

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        params = set(sig.parameters)

        if key == "actor" and "admin" not in params:
            raise ValueError(
                f"rate_limit(key='actor') requires an 'admin' parameter on {fn.__qualname__}"
            )
        if "request" not in params:
            raise ValueError(f"rate_limit requires a 'request' parameter on {fn.__qualname__}")

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Resolve the limiter key value from the call arguments.
            # FastAPI passes Depends-resolved values as keyword arguments,
            # so both ``admin`` and ``request`` should always be in kwargs
            # after dependency injection resolves them.
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            arguments = bound.arguments

            if key == "actor":
                actor: str = arguments["admin"]
                check_result = limiter.check(actor)
                log_actor = actor
            else:
                request_obj: Request = arguments["request"]
                ip = get_client_ip(request_obj)
                check_result = limiter.check(ip)
                log_actor = ip

            if not check_result:
                _logger.warning(
                    "rate_limit.throttled scope=%s actor=%s",
                    key,
                    log_actor,
                )
                return respond_err(
                    "too_many_requests",
                    status=429,
                    message="Too many requests — slow down",
                )

            return fn(*args, **kwargs)

        return wrapper

    return decorator
