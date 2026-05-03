"""Replace ``405 Method-Not-Allowed`` with ``401 Unauthorised`` on the
JSON API to close the pre-auth method-enumeration gap.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class Obscure405Middleware(BaseHTTPMiddleware):
    """Replace 405 Method-Not-Allowed with 401 on auth-gated paths.

    FastAPI checks method-match before dependency resolution, so an
    unauthenticated attacker can tell real endpoints from non-existent
    ones by seeing 401 vs 405 vs 404. That's a free API enumeration
    gift. This middleware normalises: on ``/api/*`` paths, a 405
    becomes a generic 401 with no ``Allow`` header so the method
    surface is no longer readable pre-auth.

    We deliberately scope this to ``/api/*`` only.  HTML pages at
    ``/login``, ``/dashboard``, ``/force-password-change`` etc. can
    legitimately return 405 (form misposted with the wrong verb) and
    UX callers / browsers expect that shape — converting to 401 there
    would either trigger a credential prompt or hide a genuine
    misconfiguration.  The HTML surface is also visible to anyone who
    can hit ``/login``, so the enumeration concern only really applies
    to the JSON API.

    If a future endpoint outside ``/api/*`` becomes auth-gated (e.g. a
    new ``/admin/...`` HTML console), update the prefix list here at
    that time.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if response.status_code == 405 and request.url.path.startswith("/api/"):
            body = b'{"detail":"Not authenticated"}'
            replaced = Response(
                content=body,
                status_code=401,
                media_type="application/json",
            )
            # Keep the security headers the outer middleware will apply;
            # drop the Allow header that leaked method info.
            return replaced
        return response
