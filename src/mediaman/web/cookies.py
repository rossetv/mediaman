"""Session-cookie primitives and the secure-cookie scheme resolver.

Single source of truth for everything that touches the ``session_token``
cookie: the canonical ``max_age``, the helper that writes the cookie with
the agreed attributes, and the ``is_request_secure`` resolver that decides
whether the ``Secure`` flag should be set on a given request.

Centralising the secure-cookie decision here avoids the previous drift
between :mod:`mediaman.web.routes.auth` (the original home) and the three
other route modules that needed the same answer (force-password-change,
users.passwords, users.sessions).  All four call sites now import
:func:`is_request_secure` from this module.

Allowed dependencies: :mod:`mediaman.services.rate_limit` for the
trusted-proxy CIDR helpers, plus FastAPI / Starlette primitives.  Must NOT
import from :mod:`mediaman.web.routes` or :mod:`mediaman.web.auth` — those
layers import from here.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from fastapi import Request
from starlette.responses import Response

from mediaman.services.rate_limit import peer_is_trusted, trusted_proxies

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------

# Single source of truth for the session-cookie max_age. Duplicating 86400
# across auth.py / users.py / force_password_change.py created a silent
# drift risk where one file was updated and the others weren't.
SESSION_COOKIE_MAX_AGE: int = 86400  # 24 hours


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Set the session cookie on *response* with canonical options."""
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="strict",
        max_age=SESSION_COOKIE_MAX_AGE,
        secure=secure,
    )


# ---------------------------------------------------------------------------
# Secure-cookie scheme resolver
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _secure_cookie_override() -> str | None:
    """Return the resolved value of ``MEDIAMAN_FORCE_SECURE_COOKIES``.

    Cached at first call: the env var is read once and not re-checked
    on every request.  Tests that mutate the env mid-process must call
    :func:`_secure_cookie_override.cache_clear` to invalidate.

    Returns ``"true"``, ``"false"``, or ``None`` (meaning "not set" /
    fall through to scheme detection).
    """
    raw = os.environ.get("MEDIAMAN_FORCE_SECURE_COOKIES", "").strip().lower()
    if raw in ("true", "false"):
        return raw
    return None


def is_request_secure(request: Request) -> bool:
    """Return True when the effective scheme is HTTPS.

    Resolution order:

    1. ``MEDIAMAN_FORCE_SECURE_COOKIES=true`` — unconditional yes.
    2. ``MEDIAMAN_FORCE_SECURE_COOKIES=false`` — unconditional no
       (development / plaintext loopback).
    3. Otherwise default to **secure**. Mediaman is intended to be
       served over HTTPS on any public deployment, and failing open
       to plaintext cookies is exactly the scenario that turns a
       misconfigured reverse proxy into session theft. The uvicorn
       ``proxy_headers`` / ``forwarded_allow_ips`` machinery already
       rewrites ``request.url.scheme`` to match ``X-Forwarded-Proto``
       when a trusted peer sets it, and the per-app override below
       is a belt-and-braces check: if the app genuinely sees an HTTP
       request AND the operator hasn't opted out, we STILL set the
       cookie Secure so it can't be sent on a plaintext loopback.
    """
    override = _secure_cookie_override()
    if override == "true":
        return True
    if override == "false":
        return False

    # Best-effort scheme detection: honour X-Forwarded-Proto from a
    # trusted peer if the uvicorn rewrite didn't already promote the
    # scheme (e.g. deployment didn't pass ``forwarded_allow_ips``).
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else None
    trusted = trusted_proxies()
    if peer_is_trusted(peer, trusted):
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if forwarded_proto == "https":
            return True

    # Default to True on a public-facing app — operators who genuinely
    # need plaintext (localhost-only dev) can set
    # ``MEDIAMAN_FORCE_SECURE_COOKIES=false``.
    return True


# Startup warning: log once when an operator has explicitly opted out of
# Secure cookies.  This is fine for local dev / loopback but a bad idea
# in any deployment that is reachable over the network — the admin's
# session cookie will travel in plaintext on every request.
if _secure_cookie_override() == "false":
    logger.warning(
        "auth.secure_cookies_disabled — MEDIAMAN_FORCE_SECURE_COOKIES=false; "
        "session cookies will NOT carry the Secure flag.  Only safe for "
        "loopback / development.  Unset the env var to restore the default."
    )
