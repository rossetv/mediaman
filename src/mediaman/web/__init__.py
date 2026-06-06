"""Web package — ASGI middleware orchestration for the FastAPI app.

The middleware classes themselves live in
:mod:`mediaman.web.middleware.*`; this module is the thin orchestrator
that registers them on the app in the right order.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from starlette.middleware.trustedhost import TrustedHostMiddleware

from mediaman.web.middleware.body_size import BodySizeLimitMiddleware
from mediaman.web.middleware.csrf import CSRFOriginMiddleware
from mediaman.web.middleware.force_password_change import ForcePasswordChangeMiddleware
from mediaman.web.middleware.obscure_405 import Obscure405Middleware
from mediaman.web.middleware.security_headers import SecurityHeadersMiddleware

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


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
    # Order matters: add_middleware is LIFO — the last call added is the
    # outermost middleware.  Execution order (outermost → innermost):
    #   TrustedHost: hostile Host: header rejected at the door.
    #   SecurityHeaders: wraps everything below so even 413 / 403 error
    #     responses from inner middlewares carry the security headers.
    #   BodySizeLimit: caps the body before inner middlewares spend cycles
    #     on a multi-gigabyte upload.
    #   Obscure405: rewrites /api/* 405 → 401 before CSRF sees the response.
    #   CSRFOrigin: rejects cross-origin mutations before handlers run.
    #   ForcePasswordChange: funnels flagged admins to the change page.
    app.add_middleware(ForcePasswordChangeMiddleware)
    app.add_middleware(CSRFOriginMiddleware)
    app.add_middleware(Obscure405Middleware)
    # BodySizeLimit must be added BEFORE SecurityHeaders so that
    # SecurityHeaders (BaseHTTPMiddleware) is outermost of the two.
    # Starlette's add_middleware is LIFO — last added is outermost.
    # With this ordering the execution chain is:
    #   TrustedHost → SecurityHeaders → BodySizeLimit → Obscure405 → CSRF → ForcePasswordChange
    # SecurityHeaders wraps BodySizeLimit via call_next, so 413 responses
    # (emitted directly by BodySizeLimit's raw ASGI send) are captured by
    # BaseHTTPMiddleware and have the security headers added before egress.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

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
    "register_security_middleware",
]
