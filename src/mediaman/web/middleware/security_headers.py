"""Security response headers + per-request CSP nonce middleware.

Owns the always-on response headers (CSP, X-Frame-Options, Referrer-
Policy, Permissions-Policy, COOP, HSTS) and the per-request CSP nonce
that route handlers / Jinja templates pull off ``request.state``.
"""

from __future__ import annotations

import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Content Security Policy — per-request nonce strategy.
#
# Wave 7 extracted every page-level inline ``<script>`` block to an
# external file under ``static/js/``. The only remaining inline scripts
# in templates are ``<script type="application/json">`` JSON islands,
# which are non-executable and not subject to ``script-src``. As a
# result the previous ``'unsafe-inline'`` fallback on ``script-src`` is
# no longer needed and has been dropped — a stored XSS in a Jinja
# ``|safe`` interpolation can no longer execute script content.
#
# ``style-src`` keeps ``'unsafe-inline'`` for now because several
# templates still use ``style="display:none"`` inline attributes
# (Domain 10 finding M2 / NIT). Migrating those to CSS classes is a
# separate cleanup pass; until it lands the fallback prevents a CSP
# violation on every page render.
#
# Templates expose the nonce via ``request.state.csp_nonce`` (set by
# :class:`SecurityHeadersMiddleware`).  Any future inline ``<script>``
# that genuinely has to stay inline must add
# ``nonce="{{ request.state.csp_nonce }}"`` to the tag.
#
# - ``img-src`` is an allowlist of known image CDNs:
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

    ``script-src`` no longer carries ``'unsafe-inline'``: every
    page-level inline ``<script>`` was extracted to an external file in
    Wave 7, and the only remaining inline scripts are
    ``type="application/json"`` data islands which CSP does not gate.

    ``style-src`` carries a nonce for ``<style>`` blocks. **Critical
    quirk:** in Chromium, when a nonce is present in ``style-src``,
    inline ``style="..."`` *attributes* are blocked — even if
    ``'unsafe-inline'`` is also listed. This is the documented CSP3
    behaviour for ``<style>`` BLOCKS but Chromium applies it to
    inline-style ATTRIBUTES too. Mediaman's templates use inline
    ``style="display:none"`` for modal hiding plus dynamic
    ``style="--fill-pct:..."`` for progress bars, so we publish a
    SEPARATE ``style-src-attr`` directive that allows ``'unsafe-inline'``
    without a nonce. The two directives interact correctly: blocks
    use the nonce, attributes use ``'unsafe-inline'``.

    Without ``style-src-attr`` the modals on /search and /downloads
    render with ``display:flex`` instead of ``display:none``, leaving
    the dialog stuck open on every page load (reported by an operator
    on 2026-05-03 against commit be509c1).
    """
    return (
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        f"style-src-attr 'unsafe-inline'; "
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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Attach security headers to every response and inject a per-request CSP nonce.

        Generates a fresh ``secrets.token_urlsafe(16)`` nonce, stores it on
        ``request.state.csp_nonce`` for use in Jinja templates, and weaves it
        into the outbound ``Content-Security-Policy`` header. Also sets HSTS,
        X-Frame-Options, X-Content-Type-Options, Referrer-Policy, and a
        ``Cache-Control: no-store`` header on authenticated API responses to
        prevent reverse-proxy caching of user data.
        """
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
        # Cache-Control: no-store, private on every authenticated /api
        # response so a misconfigured reverse proxy / CDN cannot serve
        # one user's data to another (Domain 02 cross-cutting). Static
        # assets keep their default cacheability — they're served from
        # ``StaticFiles`` which sets its own headers, and we only add
        # this when the response doesn't already carry a Cache-Control.
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/auth/"):
            response.headers.setdefault("Cache-Control", "no-store, private")
        if _should_emit_hsts(request):
            header = (
                _HSTS_HEADER_PRELOAD
                if os.environ.get("MEDIAMAN_HSTS_PRELOAD", "").lower() == "true"
                else _HSTS_HEADER
            )
            response.headers.setdefault("Strict-Transport-Security", header)
        return response
