"""Web package — ASGI middleware and helpers for the FastAPI app."""

from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from mediaman.auth.rate_limit import _peer_is_trusted, _trusted_proxies

# Content Security Policy.
# - ``'unsafe-inline'`` on script/style is still present because several
#   templates ship inline ``onclick=`` / ``style=`` attributes; removing
#   them is a separate refactor and should be done next.
# - ``img-src`` allows any HTTPS image source. Posters in this app come
#   from a shifting set of CDNs (TMDB, TVDB, fanart.tv, Amazon Images,
#   Plex's own metadata server, etc.) whose exact hosts we can't predict
#   because Radarr/Sonarr choose them at runtime. A strict allowlist
#   caused silent broken-image icons across Downloads/Library/Recommended.
#   Images can't execute code, so the blast radius of an overly-broad
#   img-src is limited to pixel tracking if an integrated service ever
#   got compromised and started serving adversary-chosen URLs — which
#   is already outside our trust model (admins are trusted).
# - ``object-src 'none'`` defangs plugin-based XSS.
# - ``frame-ancestors 'none'`` + ``X-Frame-Options: DENY`` belt-and-braces
#   clickjacking defence.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-src https://www.youtube.com; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# Always-on headers applied to every response.
_STATIC_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "interest-cohort=(), geolocation=(), camera=(), microphone=()",
    "Content-Security-Policy": _CSP,
    "Cross-Origin-Opener-Policy": "same-origin",
}

# HSTS — 2 years, includeSubDomains. ``preload`` is set conservatively only
# when the operator opts in via env var; submitting to the HSTS preload
# list is a one-way door and should be an explicit decision.
_HSTS_HEADER = "max-age=63072000; includeSubDomains"
_HSTS_HEADER_PRELOAD = "max-age=63072000; includeSubDomains; preload"


def _should_emit_hsts(request: Request) -> bool:
    """Return True when the browser-visible protocol is HTTPS.

    Mirrors the logic in :func:`auth_routes._is_request_secure` but
    without importing from it (to avoid a circular import). HSTS on a
    plaintext response is harmless — browsers ignore it — but emitting
    it unconditionally in the normal case protects against downgrade
    when a reverse proxy misroutes.
    """
    override = os.environ.get("MEDIAMAN_FORCE_SECURE_COOKIES", "").strip().lower()
    if override == "true":
        return True
    if override == "false":
        return False
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else None
    trusted = _trusted_proxies()
    if _peer_is_trusted(peer, trusted):
        forwarded_proto = (
            request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        )
        if forwarded_proto == "https":
            return True
    # Default to True for the public-facing app; operators can opt out
    # via MEDIAMAN_FORCE_SECURE_COOKIES=false for genuine dev loopback.
    return True


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security response headers to every HTTP response.

    Adds clickjacking, MIME-type-sniffing, referrer-leak, CSP, and
    Permissions-Policy defences. HSTS is emitted whenever the
    browser-visible scheme is HTTPS (or when the operator forces it).
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        response = await call_next(request)
        for name, value in _STATIC_HEADERS.items():
            response.headers.setdefault(name, value)
        # Hide server banner (FastAPI/uvicorn leaks nothing sensitive,
        # but there's no reason to advertise).
        response.headers["Server"] = "mediaman"
        if _should_emit_hsts(request):
            header = (
                _HSTS_HEADER_PRELOAD
                if os.environ.get("MEDIAMAN_HSTS_PRELOAD", "").lower() == "true"
                else _HSTS_HEADER
            )
            response.headers.setdefault("Strict-Transport-Security", header)
        return response


# State-changing methods that must carry an Origin/Referer from the
# same host. GET/HEAD/OPTIONS are never state-changing in a correct
# REST app.
_CSRF_PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Routes that must accept cross-origin POSTs (they are token-auth'd and
# get hit from email clients where the browser's Origin is whichever
# mail host the user used). The token IS the authorisation — SameSite
# doesn't apply to in-email links because the user clicks through.
# All entries end with ``/`` (or explicitly cover the path via
# _CSRF_EXEMPT_EXACT) so a future route starting with the same substring
# can't accidentally inherit the exemption.
_CSRF_EXEMPT_PREFIXES = (
    "/keep/",
    "/download/",  # /download/{token} + /download/{token} POST
    "/unsubscribe/",
)
_CSRF_EXEMPT_EXACT = frozenset({
    "/unsubscribe",  # bare path without query string
})


def _normalise_host(netloc: str) -> str:
    """Return host without default port (80 for http, 443 for https).

    Without this, an ``Origin: https://mediaman.example:443`` header
    (which some legacy webviews emit) fails the same-origin check
    against ``request.url.netloc == "mediaman.example"``. Scheme is
    ignored because the middleware already runs behind HTTPS in
    production and the Origin header's scheme should match the
    request URL regardless.
    """
    host = netloc.lower().strip()
    if host.endswith(":443") or host.endswith(":80"):
        host = host.rsplit(":", 1)[0]
    return host


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests whose Origin/Referer mismatches the host.

    Belt-and-braces defence on top of ``SameSite=Strict`` session
    cookies. SameSite covers modern browsers; Origin checks cover
    legacy browsers, in-app webviews, and anything that might ship
    cookies without honouring the SameSite attribute.

    The check is intentionally narrow: only POST/PUT/PATCH/DELETE
    from non-same-origin origins are refused, and only for paths
    that aren't in the explicit exempt list (where the token, not
    the cookie, is the authorisation).
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        if request.method not in _CSRF_PROTECTED_METHODS:
            return await call_next(request)

        path = request.url.path
        if path in _CSRF_EXEMPT_EXACT:
            return await call_next(request)
        for prefix in _CSRF_EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        expected_host = _normalise_host(request.url.netloc or "")

        # If neither header is present, assume a non-browser client
        # (curl, scripts). Those callers don't have CSRF exposure —
        # they don't ride a victim's cookie. Let them through; the
        # authentication dependency will reject if they're unauth.
        if not origin and not referer:
            return await call_next(request)

        def _host_of(url: str) -> str:
            try:
                from urllib.parse import urlparse
                return _normalise_host(urlparse(url).netloc or "")
            except Exception:
                return ""

        if origin and _host_of(origin) != expected_host:
            return Response(status_code=403, content=b"CSRF: origin mismatch")
        if not origin and referer and _host_of(referer) != expected_host:
            return Response(status_code=403, content=b"CSRF: referer mismatch")
        return await call_next(request)


class ForcePasswordChangeMiddleware(BaseHTTPMiddleware):
    """Funnel flagged admins to /force-password-change.

    When an admin signs in with a plaintext password that fails the
    current strength policy, ``auth_routes.login_submit`` flips the
    ``must_change_password`` flag on their row. Any subsequent
    request that carries their session cookie gets intercepted here:

    - ``GET`` requests for anything other than the force-change page,
      static assets, logout, or the login page itself are 302-
      redirected to ``/force-password-change``.
    - ``POST`` / state-changing methods get a 403 JSON response so
      JS callers see a clean failure rather than a redirect.

    The check is cheap: cookie lookup + single-row SELECT; no
    validation, no HMAC, no crypto.
    """

    # Paths that are always allowed even when a user is flagged —
    # the force-change page itself, its POST, static assets, logout,
    # and the login page (so the user can switch accounts if they
    # don't remember their own password).
    _ALLOWED_PREFIXES = (
        "/force-password-change",
        "/static/",
        "/login",
        "/api/auth/logout",
    )

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        token = request.cookies.get("session_token")
        if not token:
            return await call_next(request)

        path = request.url.path
        if any(path == p or path.startswith(p) for p in self._ALLOWED_PREFIXES):
            return await call_next(request)

        # Cheap check — avoid importing the DB layer at module load
        # to keep this middleware testable in isolation.
        try:
            from mediaman.auth.rate_limit import get_client_ip
            from mediaman.auth.session import (
                user_must_change_password,
                validate_session,
            )
            from mediaman.db import get_db
        except Exception:
            return await call_next(request)

        try:
            conn = get_db()
        except RuntimeError:
            return await call_next(request)

        user_agent = request.headers.get("user-agent", "")
        client_ip = get_client_ip(request)
        username = validate_session(
            conn, token, user_agent=user_agent, client_ip=client_ip,
        )
        if username is None:
            return await call_next(request)

        if not user_must_change_password(conn, username):
            return await call_next(request)

        # Flagged: funnel.
        if request.method == "GET":
            return Response(
                status_code=302,
                headers={"Location": "/force-password-change"},
            )
        import json as _json
        body = _json.dumps({
            "detail": "password_change_required",
            "message": "You must change your password before continuing.",
            "redirect": "/force-password-change",
        }).encode()
        return Response(
            content=body,
            status_code=403,
            media_type="application/json",
        )


class Obscure405Middleware(BaseHTTPMiddleware):
    """Replace 405 Method-Not-Allowed with 401 on auth-gated paths.

    FastAPI checks method-match before dependency resolution, so an
    unauthenticated attacker can tell real endpoints from non-existent
    ones by seeing 401 vs 405 vs 404. That's a free API enumeration
    gift. This middleware normalises: on ``/api/*`` paths, a 405
    becomes a generic 401 with no ``Allow`` header so the method
    surface is no longer readable pre-auth.

    We only do this for ``/api/*`` — HTML pages at ``/login`` can
    legitimately return 405 and callers expect that shape.
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        response = await call_next(request)
        if (
            response.status_code == 405
            and request.url.path.startswith("/api/")
        ):
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


def register_security_middleware(app) -> None:
    """Register security middleware on a FastAPI/Starlette app.

    Exposed as a helper so the app factory can wire the middleware without
    having to import Starlette primitives directly.
    """
    # Order matters: outermost is added last. CSRF check runs first so
    # rejected requests never hit the handler. 405-obscure runs second
    # so downstream security headers wrap its replacement response too.
    # Security headers wrap everything.
    # Order (outermost last): ForcePasswordChange runs first so a
    # flagged admin is funnelled immediately; CSRF + Obscure405
    # apply next; SecurityHeaders wraps everything.
    app.add_middleware(ForcePasswordChangeMiddleware)
    app.add_middleware(CSRFOriginMiddleware)
    app.add_middleware(Obscure405Middleware)
    app.add_middleware(SecurityHeadersMiddleware)


__all__ = [
    "SecurityHeadersMiddleware",
    "CSRFOriginMiddleware",
    "Obscure405Middleware",
    "ForcePasswordChangeMiddleware",
    "register_security_middleware",
]
