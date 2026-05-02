"""Web package — ASGI middleware and helpers for the FastAPI app."""

from __future__ import annotations

import logging
import os
import re
import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


# Content Security Policy — per-request nonce strategy.
#
# - A fresh ``nonce-<base64url>`` value is minted per request and added to
#   ``script-src`` and ``style-src`` alongside the existing
#   ``'unsafe-inline'``.  CSP3-aware browsers ignore ``'unsafe-inline'``
#   when a ``'nonce-...'`` is present, so any inline ``<script>`` or
#   ``<style>`` block that doesn't carry the matching ``nonce="..."``
#   attribute will be rejected.  Legacy browsers that don't support
#   nonces fall back to ``'unsafe-inline'`` so existing inline blocks
#   continue to render until they are migrated to either external files
#   or to nonce-marked blocks (TODO H65).
#
#   Templates expose the nonce via ``request.state.csp_nonce`` (set by
#   :class:`SecurityHeadersMiddleware`).  Inline scripts that need to
#   stay inline should add ``nonce="{{ request.state.csp_nonce }}"``.
#
# - ``img-src`` is an allowlist of known image CDNs (finding 20):
#   * 'self'           — /api/poster proxy + static assets
#   * data: blob:      — inline data URIs and object URLs used by JS
#   * image.tmdb.org   — TMDB poster/backdrop images
#   * i.ytimg.com      — YouTube thumbnail images
#   * www.gravatar.com — Gravatar profile images (admin avatars, if any)
#   * mediacover.radarr.video mediacover.sonarr.video — Radarr/Sonarr
#     fallback for poster remoteUrls when TMDB is unreachable
#   Previous value was ``https:`` (any HTTPS image host).  The tighter
#   allowlist reduces the pixel-tracking surface to known services.
# - ``object-src 'none'`` defangs plugin-based XSS.
# - ``frame-ancestors 'none'`` + ``X-Frame-Options: DENY`` belt-and-braces
#   clickjacking defence.
_CSP_STATIC_DIRECTIVES = (
    "default-src 'self'; "
    "img-src 'self' data: blob: "
    "https://image.tmdb.org "
    "https://i.ytimg.com "
    "https://www.gravatar.com "
    "https://mediacover.radarr.video "
    "https://mediacover.sonarr.video; "
    "connect-src 'self'; "
    "frame-src https://www.youtube.com; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _build_csp(nonce: str) -> str:
    """Return the per-request CSP header text with *nonce* threaded in.

    The nonce is added to both ``script-src`` and ``style-src``; the
    existing ``'unsafe-inline'`` is retained as the legacy fallback so
    templates that still ship un-noncified inline blocks keep working
    on CSP2-only browsers.
    """
    return (
        f"script-src 'self' 'nonce-{nonce}' 'unsafe-inline'; "
        f"style-src 'self' 'nonce-{nonce}' 'unsafe-inline'; "
        f"{_CSP_STATIC_DIRECTIVES}"
    )


# Backward-compatible representative CSP value for tests and tooling
# that need a static string snapshot of the policy.  The placeholder
# obviously is not a real per-request value — call :func:`_build_csp`
# for the runtime header.
_CSP = _build_csp("placeholder")

# Always-on headers applied to every response.  CSP is added per-request
# in :class:`SecurityHeadersMiddleware` because the nonce changes on
# every dispatch.
_STATIC_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "interest-cohort=(), geolocation=(), camera=(), microphone=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# HSTS — 2 years, includeSubDomains. ``preload`` is set conservatively only
# when the operator opts in via env var; submitting to the HSTS preload
# list is a one-way door and should be an explicit decision.
_HSTS_HEADER = "max-age=63072000; includeSubDomains"
_HSTS_HEADER_PRELOAD = "max-age=63072000; includeSubDomains; preload"


def _should_emit_hsts(request: Request) -> bool:
    """Return True only when the operator has explicitly enabled HSTS
    AND the current request is genuinely HTTPS.

    HSTS is a *one-way door*: once a browser caches the
    ``Strict-Transport-Security`` header for ``max-age=63072000`` (2
    years), it will refuse plaintext access to that origin for the full
    window even after operators take the header back down.  A
    misconfigured initial deploy that briefly serves HTTP can therefore
    lock real users out of the host for two years.

    Because of that one-way blast radius this function is now
    deliberately *fail-closed*:

    - Emission requires ``MEDIAMAN_HSTS_ENABLED=true`` to be set
      explicitly.  There is no implicit "default on" — operators must
      opt in once they have confirmed the deployment is end-to-end
      HTTPS.
    - Even with the env flag on, the header is only attached when the
      request itself is HTTPS.  Uvicorn rewrites ``request.url.scheme``
      from ``X-Forwarded-Proto`` when ``proxy_headers=True`` (only set
      when the operator has supplied ``MEDIAMAN_TRUSTED_PROXIES``), so
      this single check covers both direct-TLS and reverse-proxy
      deployments.

    The legacy ``MEDIAMAN_FORCE_SECURE_COOKIES`` env var is still
    honoured as a hard ``false`` override (i.e. it can disable HSTS
    even when ``MEDIAMAN_HSTS_ENABLED=true``) so an operator with the
    old toggle in place doesn't get a surprise upgrade.
    """
    if os.environ.get("MEDIAMAN_FORCE_SECURE_COOKIES", "").strip().lower() == "false":
        return False
    if os.environ.get("MEDIAMAN_HSTS_ENABLED", "").strip().lower() != "true":
        return False
    return request.url.scheme == "https"


# NOTE on ``BaseHTTPMiddleware`` (Starlette):
#
# Starlette's docs explicitly recommend pure-ASGI middleware over
# :class:`BaseHTTPMiddleware` for production use.  ``BaseHTTPMiddleware``
# buffers request/response bodies in memory and runs the inner app on a
# detached task, which complicates streaming, timeouts, and cancellation
# semantics.  Three of the four middleware below
# (:class:`SecurityHeadersMiddleware`, :class:`CSRFOriginMiddleware`,
# :class:`Obscure405Middleware`, :class:`ForcePasswordChangeMiddleware`)
# still use it because their behaviour is well-tested against the
# request/response object model and the migration risk outweighs the
# theoretical streaming benefit for these short, header-centric paths
# (no body inspection, no streaming responses).
#
# The new :class:`BodySizeLimitMiddleware` is pure ASGI by necessity —
# it must enforce a cap as bytes stream in.  Future migrations of the
# others should preserve every existing behaviour and test before
# flipping over.
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security response headers to every HTTP response.

    Adds clickjacking, MIME-type-sniffing, referrer-leak, CSP, and
    Permissions-Policy defences. HSTS is emitted whenever the
    browser-visible scheme is HTTPS (or when the operator forces it).

    Mints a fresh CSP nonce for each request and stashes it on
    ``request.state.csp_nonce`` so route handlers and Jinja templates
    can pull it for inline ``<script nonce="...">`` /
    ``<style nonce="...">`` blocks.  The same nonce is woven into the
    ``Content-Security-Policy`` header on the outbound response so the
    browser accepts the marked inline blocks.
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]  # Starlette types call_next as a positional-only callable with no public type alias; annotating it fully would require importing private starlette internals
        # 16 random bytes → 22 base64url chars: enough entropy that an
        # attacker cannot brute-force the nonce within the lifetime of
        # a single response, but short enough not to bloat every inline
        # block by more than a couple of dozen characters.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)
        for name, value in _STATIC_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers.setdefault("Content-Security-Policy", _build_csp(nonce))
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
    ``str(request.url)``) or a bare ``host[:port]`` netloc.  The result
    is suitable for direct equality comparison so the CSRF middleware
    can require both the scheme AND host of the origin/referer to match
    the request URL — finding 11.

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
        if ":" in host:
            host_with_port = f"[{host}]:{port}"
        else:
            host_with_port = f"{host}:{port}"
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

    Both **scheme** and **host** must match — finding 11.  An attacker
    page on ``http://example.com`` must not be able to submit to
    ``https://example.com``; the previous host-only check accepted
    those cross-scheme requests.

    The check is intentionally narrow: only POST/PUT/PATCH/DELETE
    from non-same-origin origins are refused, and only for routes
    that aren't in :data:`_CSRF_EXEMPT_ROUTES` (where the token, not
    the cookie, is the authorisation).
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]  # same as SecurityHeadersMiddleware — call_next has no stable public type in Starlette
        if request.method not in _CSRF_PROTECTED_METHODS:
            return await call_next(request)

        path = request.url.path
        if _csrf_route_is_exempt(request.method, path):
            return await call_next(request)

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        request_scheme = (request.url.scheme or "").lower()
        expected = _normalise_origin(request.url.netloc or "", default_scheme=request_scheme)

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

        def _origin_of(url: str) -> tuple[str, str]:
            try:
                return _normalise_origin(url, default_scheme=request_scheme)
            except Exception:
                return ("", "")

        if origin and _origin_of(origin) != expected:
            return Response(status_code=403, content=b"CSRF: origin mismatch")
        if not origin and referer and _origin_of(referer) != expected:
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
    # the login page (so the user can switch accounts if they don't
    # remember their own password), and the kubelet/Docker probes
    # (which carry no session cookie of their own but might collide
    # with one from a stale browser tab on the same origin and would
    # otherwise be redirected away from a 200 healthcheck reply).
    _ALLOWED_PREFIXES = (
        "/force-password-change",
        "/static/",
        "/login",
        "/api/auth/logout",
        "/healthz",
        "/readyz",
    )

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]  # same as SecurityHeadersMiddleware — call_next has no stable public type in Starlette
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
            conn,
            token,
            user_agent=user_agent,
            client_ip=client_ip,
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

        body = _json.dumps(
            {
                "detail": "password_change_required",
                "message": "You must change your password before continuing.",
                "redirect": "/force-password-change",
            }
        ).encode()
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

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]  # same as SecurityHeadersMiddleware — call_next has no stable public type in Starlette
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


def register_security_middleware(app) -> None:
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
    "BodySizeLimitMiddleware",
    "CSRFOriginMiddleware",
    "ForcePasswordChangeMiddleware",
    "Obscure405Middleware",
    "SecurityHeadersMiddleware",
    "register_security_middleware",
]
