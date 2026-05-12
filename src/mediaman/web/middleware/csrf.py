"""Origin/Referer-based CSRF defence for state-changing requests.

Belt-and-braces on top of ``SameSite=Strict`` session cookies — covers
legacy browsers, in-app webviews, and anything that might ship cookies
without honouring the SameSite attribute. See
:class:`CSRFOriginMiddleware` for the full rationale.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# State-changing methods that must carry an Origin/Referer from the
# same host. GET/HEAD/OPTIONS are never state-changing in a correct
# REST app.
_CSRF_PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Explicit allowlist of (method, path-pattern) pairs that bypass the
# Origin/Referer check.  Each entry MUST correspond to a route whose
# authorisation does not ride on the session cookie — typically routes
# that are HMAC-token-authenticated and arrive from a mail client where
# the browser-supplied Origin is whichever webmail host the recipient
# happens to use.
#
# Switching from a prefix-based exemption (the original design) to an
# explicit (method, regex) allowlist closes a sharp edge: previously a
# *new* POST added under one of the exempt prefixes (``/download/...``,
# ``/keep/...``, ``/unsubscribe/...``) would silently inherit the
# exemption with no compile-time signal.  Adding a new exempt route
# now requires editing this list, which is reviewable and grep-able.
#
# Patterns are anchored with ``^`` and ``$`` and use ``[^/]+`` to match
# a single token segment — they will NOT match nested paths like
# ``/download/abc/extra`` even if those happen to share the prefix.
_CSRF_EXEMPT_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # POST /download/{token} — public download submit.  Authorised by
    # an HMAC-signed download token in the URL; SameSite=Strict cookies
    # would block legitimate clickthroughs from webmail.
    ("POST", re.compile(r"^/download/[^/]+$")),
    # POST /keep/{token} — public snooze for scheduled deletions.
    # Authorised by an HMAC-signed keep token in the URL.
    # NB: ``POST /api/keep/{token}/forever`` is NOT exempt; it sits
    # under ``/api/...`` and requires an admin session.
    ("POST", re.compile(r"^/keep/[^/]+$")),
    # POST /unsubscribe — public unsubscribe confirmation.  Authorised
    # by an HMAC-signed token submitted as a form field.
    ("POST", re.compile(r"^/unsubscribe$")),
)


def _csrf_route_is_exempt(method: str, path: str) -> bool:
    """Return True iff (*method*, *path*) is in the explicit exempt list."""
    for exempt_method, pattern in _CSRF_EXEMPT_ROUTES:
        if exempt_method == method and pattern.match(path):
            return True
    return False


_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _normalise_origin(value: str, default_scheme: str | None = None) -> tuple[str, str]:
    """Return ``(scheme, host_without_default_port)`` for *value*.

    *value* may be a full URL (``Origin``/``Referer`` header values, or
    ``str(request.url)``) or a bare ``host[:port]`` netloc. The result
    is suitable for direct equality comparison so the CSRF middleware
    can require both the scheme AND host of the origin/referer to match
    the request URL.

    Two correctness fixes over the previous prefix-stripping logic:

    1. **IPv6** — ``urlsplit("https://[2001:db8::1]:443").hostname``
       returns ``"2001:db8::1"`` cleanly.  The previous
       ``rsplit(":", 1)[0]`` chopped the trailing ``::1`` off and
       produced ``"[2001:db8"`` (corrupted host that couldn't match
       anything).
    2. **Non-default ports** — ``https://example.com:8443`` previously
       failed the ``endswith(":443")`` check and survived as
       ``"example.com:8443"``, never matching ``"example.com"``.  The
       new logic only strips the port when it equals the *default* for
       the scheme; non-default ports are preserved so a request on
       ``:8443`` requires an Origin on ``:8443`` (correct).

    *default_scheme* lets a caller normalise a bare netloc (no
    ``scheme://`` prefix) by supplying the scheme to assume.  When the
    value already includes a scheme, *default_scheme* is ignored.
    """
    raw = value.strip()
    if not raw:
        return ("", "")

    # If the value has no scheme, urlsplit will treat the whole thing
    # as the path; we want netloc semantics, so prefix a placeholder
    # scheme to make urlsplit cooperate, then carry the caller-supplied
    # default_scheme back into the result.
    if "://" in raw:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
    else:
        parts = urlsplit("http://" + raw)
        scheme = (default_scheme or "").lower()

    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        host_with_port = host
    elif port is not None:
        # IPv6 hosts must keep their bracketing when re-stitched with a
        # port, so something like "::1" gets serialised as "[::1]:8080"
        # rather than the ambiguous "::1:8080".
        host_with_port = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    else:
        host_with_port = host
    return (scheme, host_with_port)


def _normalise_host(netloc: str) -> str:
    """Return host (with non-default port) for a bare ``netloc`` string.

    Kept for backward compatibility with callers and tests that only
    care about the host portion.  New code should prefer
    :func:`_normalise_origin` so the scheme can be compared too.

    Without a scheme to anchor to, a bare ``"example.com:443"`` is
    ambiguous — port 443 is the default for ``https`` but not for
    ``http``.  Tests for this shim assume the historical "production
    is HTTPS" assumption; fold the netloc through both default schemes
    and strip the port when it equals either default.
    """
    # Try both common defaults; if either yields a stripped host, use
    # that.  Otherwise return the host[:port] as-is.
    https_host = _normalise_origin(netloc, default_scheme="https")[1]
    http_host = _normalise_origin(netloc, default_scheme="http")[1]
    # The shorter result is the one that successfully stripped the port.
    return min((https_host, http_host), key=len)


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests whose Origin/Referer mismatches the host.

    Belt-and-braces defence on top of ``SameSite=Strict`` session
    cookies. SameSite covers modern browsers; Origin checks cover
    legacy browsers, in-app webviews, and anything that might ship
    cookies without honouring the SameSite attribute.

    The comparison is **host-only**, not (scheme, host). A previous
    hardening that compared scheme and host broke real reverse-proxy
    deployments where uvicorn sees ``request.url.scheme == "http"`` but
    the browser is on ``https://`` and sets ``Origin: https://...``.
    Trusting ``X-Forwarded-Proto`` to rewrite the scheme is itself a
    footgun (it requires ``MEDIAMAN_TRUSTED_PROXIES`` to be configured
    first), and the cross-scheme attack being guarded against is already
    closed by ``Secure`` cookie flag (browser refuses to send the cookie
    over HTTP) and ``SameSite=Strict`` on the session cookie.

    The check is intentionally narrow: only POST/PUT/PATCH/DELETE
    from non-same-origin origins are refused, and only for routes
    that aren't in :data:`_CSRF_EXEMPT_ROUTES` (where the token, not
    the cookie, is the authorisation).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Enforce same-origin CSRF policy on state-changing requests.

        Passes the request through unchanged for safe methods (GET, HEAD,
        OPTIONS) and for CSRF-exempt routes. For all other requests it
        compares the ``Origin`` or ``Referer`` header host against the
        request host and rejects mismatches with a 403 response.
        """
        if request.method not in _CSRF_PROTECTED_METHODS:
            return await call_next(request)

        path = request.url.path
        if _csrf_route_is_exempt(request.method, path):
            return await call_next(request)

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        # Compare hosts only (see class docstring for why scheme equality
        # was reverted).  Use ``_normalise_origin`` for the IPv6 + default-
        # port handling, but ignore the scheme component of the returned
        # tuple by indexing ``[1]``.
        request_scheme = (request.url.scheme or "").lower()
        expected_host = _normalise_origin(request.url.netloc or "", default_scheme=request_scheme)[
            1
        ]

        # If neither header is present AND no session cookie is present,
        # assume a non-browser API client (curl, scripts). Those callers
        # don't have CSRF exposure — they don't ride a victim's cookie.
        # However, if a session_token cookie IS present and neither Origin
        # nor Referer was sent, the request is ambiguous: a browser that
        # drops both headers can still carry the session cookie and be
        # exploited via a CSRF form. Reject those to close the gap.
        if not origin and not referer:
            has_session = bool(request.cookies.get("session_token"))
            if has_session:
                return Response(
                    status_code=403,
                    content=b"CSRF: origin required for cookie-authenticated requests",
                )
            return await call_next(request)

        def _host_of(url: str) -> str:
            try:
                return _normalise_origin(url, default_scheme=request_scheme)[1]
            except ValueError:
                # Malformed Origin/Referer header — urlsplit / .port raised
                # for an invalid IPv6 host or a non-numeric port. Treat as
                # an empty host so the CSRF comparison fails closed (the
                # request will be rejected as a mismatch).
                return ""

        if origin and _host_of(origin) != expected_host:
            return Response(status_code=403, content=b"CSRF: origin mismatch")
        if not origin and referer and _host_of(referer) != expected_host:
            return Response(status_code=403, content=b"CSRF: referer mismatch")
        return await call_next(request)
