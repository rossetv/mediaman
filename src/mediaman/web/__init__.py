"""Web package — ASGI middleware orchestration for the FastAPI app.

The middleware classes themselves live in
:mod:`mediaman.web.middleware.*`; this module is the thin orchestrator
that registers them on the app in the right order plus a back-compat
re-export surface for callers and tests that still import the symbols
from :mod:`mediaman.web` directly.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from starlette.middleware.trustedhost import TrustedHostMiddleware

if TYPE_CHECKING:
    from fastapi import FastAPI

from mediaman.web.middleware.body_size import (
    _DEFAULT_MAX_REQUEST_BYTES,
    BodySizeLimitMiddleware,
    _resolve_max_request_bytes,
    _send_413,
)
from mediaman.web.middleware.csrf import (
    _CSRF_EXEMPT_ROUTES,
    _CSRF_PROTECTED_METHODS,
    _DEFAULT_PORTS,
    CSRFOriginMiddleware,
    _csrf_route_is_exempt,
    _normalise_host,
    _normalise_origin,
)
from mediaman.web.middleware.force_password_change import ForcePasswordChangeMiddleware
from mediaman.web.middleware.obscure_405 import Obscure405Middleware
from mediaman.web.middleware.security_headers import (
    _CSP,
    _CSP_STATIC_DIRECTIVES,
    _HSTS_HEADER,
    _HSTS_HEADER_PRELOAD,
    _STATIC_HEADERS,
    SecurityHeadersMiddleware,
    _build_csp,
    _should_emit_hsts,
)

logger = logging.getLogger("mediaman.web")


def _parse_allowed_hosts(raw: str | None) -> list[str]:
    """Parse ``MEDIAMAN_ALLOWED_HOSTS`` into a Starlette ``allowed_hosts`` list.

    The env var accepts a comma-separated list of hostnames (with or
    without surrounding whitespace).  An empty / unset value is
    interpreted as ``["*"]`` — i.e. accept any Host header — to keep
    backward compatibility with deployments that have not yet pinned a
    hostname.  A ``*`` entry inside the list is also passed through so
    operators can keep wildcard mode but still re-export the var with a
    comment.

    Hostnames are case-folded because the HTTP host comparison Starlette
    performs is case-insensitive in spec but case-sensitive in code.
    """
    if not raw:
        return ["*"]
    hosts = [h.strip().lower() for h in raw.split(",") if h.strip()]
    return hosts or ["*"]


def register_security_middleware(app: FastAPI) -> None:
    """Register security middleware on a FastAPI/Starlette app.

    Exposed as a helper so the app factory can wire the middleware without
    having to import Starlette primitives directly.
    """
    # Order matters: outermost is added last. CSRF check runs first so
    # rejected requests never hit the handler. 405-obscure runs second
    # so downstream security headers wrap its replacement response too.
    # Security headers wrap everything.
    # Order (outermost last):
    #   ForcePasswordChange runs first so a flagged admin is funnelled
    #     immediately;
    #   CSRF + Obscure405 apply next;
    #   SecurityHeaders wraps everything below;
    #   BodySizeLimit caps the body before any of the above spend
    #     cycles on a multi-gigabyte upload;
    #   TrustedHost is outermost so a hostile Host header is rejected
    #     at the door.
    app.add_middleware(ForcePasswordChangeMiddleware)
    app.add_middleware(CSRFOriginMiddleware)
    app.add_middleware(Obscure405Middleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)

    raw_allowed_hosts = os.environ.get("MEDIAMAN_ALLOWED_HOSTS", "")
    allowed_hosts = _parse_allowed_hosts(raw_allowed_hosts)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    if allowed_hosts == ["*"]:
        # The default of ``*`` keeps the door open for operators who
        # haven't yet pinned a hostname, but a Host-header attacker
        # can poison anything we build from ``request.url`` (CSRF
        # comparisons, cookie domains, generated absolute links).
        # Log once at startup so the gap is at least *visible*.
        logger.warning(
            "MEDIAMAN_ALLOWED_HOSTS is unset; the app will accept any Host: header. "
            "Set MEDIAMAN_ALLOWED_HOSTS=mediaman.example.com,... to lock this down."
        )


__all__ = [
    # Re-exported helpers for back-compat with callers/tests that
    # imported them from :mod:`mediaman.web` before the split.
    "_CSP",
    "_CSP_STATIC_DIRECTIVES",
    "_CSRF_EXEMPT_ROUTES",
    "_CSRF_PROTECTED_METHODS",
    "_DEFAULT_MAX_REQUEST_BYTES",
    "_DEFAULT_PORTS",
    "_HSTS_HEADER",
    "_HSTS_HEADER_PRELOAD",
    "_STATIC_HEADERS",
    "BodySizeLimitMiddleware",
    "CSRFOriginMiddleware",
    "ForcePasswordChangeMiddleware",
    "Obscure405Middleware",
    "SecurityHeadersMiddleware",
    "_build_csp",
    "_csrf_route_is_exempt",
    "_normalise_host",
    "_normalise_origin",
    "_parse_allowed_hosts",
    "_resolve_max_request_bytes",
    "_send_413",
    "_should_emit_hsts",
    "register_security_middleware",
]
