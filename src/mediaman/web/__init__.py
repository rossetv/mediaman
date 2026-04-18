"""Web package — ASGI middleware and helpers for the FastAPI app."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Content Security Policy — `unsafe-inline` is required for now because a
# handful of templates still use inline onclick/style attributes. The policy
# still blocks remote scripts, framing, plugins, base-uri hijacks, and
# unexpected form posts, and should be tightened once the inline bits are
# refactored.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "frame-src https://www.youtube.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# Always-on headers applied to every response.
_STATIC_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "interest-cohort=()",
    "Content-Security-Policy": _CSP,
}

# HSTS header — only meaningful when the request was served over HTTPS, so
# we attach it conditionally.  When mediaman is deployed behind a TLS-
# terminating reverse proxy the scheme will be rewritten via forwarded
# headers before this middleware runs (FastAPI reads X-Forwarded-Proto via
# uvicorn's ``proxy_headers``), so ``request.url.scheme`` reflects the
# browser-facing protocol.
_HSTS_HEADER = "max-age=63072000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security response headers to every HTTP response.

    Adds a conservative set of headers covering clickjacking, MIME-type
    sniffing, referrer leakage, FLoC, and a permissive CSP that still
    blocks third-party script injection. HSTS is added only when the
    request was served over HTTPS so HTTP-only deployments can still be
    reached in development.
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        response = await call_next(request)
        for name, value in _STATIC_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", _HSTS_HEADER)
        return response


def register_security_middleware(app) -> None:
    """Register :class:`SecurityHeadersMiddleware` on a FastAPI/Starlette app.

    Exposed as a helper so the app factory can wire the middleware without
    having to import Starlette primitives directly.
    """
    app.add_middleware(SecurityHeadersMiddleware)


__all__ = [
    "SecurityHeadersMiddleware",
    "register_security_middleware",
]
