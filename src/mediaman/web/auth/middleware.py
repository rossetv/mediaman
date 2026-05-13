"""FastAPI authentication dependency functions.

Relocated from ``mediaman.auth.middleware`` to ``mediaman.web.auth.middleware``
during the Ring-2 restructure.  Back-compat re-exports remain in
:mod:`mediaman.auth.middleware` so existing imports continue to work.

Exports four FastAPI/callable dependency functions that all callers should
prefer over raw ``validate_session`` calls — they guarantee fingerprint
binding (User-Agent + client IP) is consistently applied:

:func:`get_current_admin`
    Strict dependency: returns the username string or raises ``401``.
    Use in endpoints that must be authenticated.

:func:`get_optional_admin`
    Soft dependency: returns username or ``None``, never raises.
    Use in endpoints that adjust their response for authenticated
    vs. anonymous visitors (e.g. HTMX partials with extra controls).

:func:`get_optional_admin_from_token`
    Non-FastAPI variant of the above; accepts a raw token string and
    an optional :class:`starlette.requests.Request`.  Used in routes
    that extract the cookie manually before the dependency injection
    layer runs.

:func:`resolve_page_session`
    Page-route helper that returns ``(username, conn)`` on success or a
    ``RedirectResponse("/login", 302)`` for unauthenticated callers.
    Centralises the cookie → ``validate_session`` → redirect dance every
    page handler needs.

:func:`is_admin`
    Convenience predicate over :func:`get_optional_admin_from_token` that
    pulls the ``session_token`` cookie off the request itself.  Used by
    page-rendering routes that toggle admin-only UI affordances on or off
    without rejecting anonymous callers (keep and kept).
"""

from __future__ import annotations

import sqlite3

from fastapi import Cookie, HTTPException, Request
from starlette.responses import RedirectResponse

from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth.session_store import validate_session

# Alias for the resolve_page_session return union, used for type annotations at call sites.
PageSession = tuple[str, sqlite3.Connection] | RedirectResponse


def get_current_admin(
    request: Request,
    session_token: str | None = Cookie(default=None),
) -> str:
    """FastAPI dependency — returns username or raises 401.

    Passes the current request's ``User-Agent`` and client IP into
    ``validate_session`` so session fingerprint binding is enforced on
    every request. Uniform ``Not authenticated`` error (no separate
    "Session expired" leak).
    """
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    # ``or None`` ensures an empty header is treated as missing. Previously
    # we passed ``""`` through, which tripped the fingerprint check's
    # ``is not None`` guard against a blank UA.
    user_agent = request.headers.get("user-agent") or None
    client_ip = get_client_ip(request) or None
    username = validate_session(
        conn,
        session_token,
        user_agent=user_agent,
        client_ip=client_ip,
    )
    if username is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def get_optional_admin(
    request: Request,
    session_token: str | None = Cookie(default=None),
) -> str | None:
    """FastAPI dependency — returns username or None (no error).

    ``request`` is required so fingerprint + client-IP binding can be
    checked. Callers that want to skip the Request dependency can use
    :func:`get_optional_admin_from_token` directly with a raw token.
    """
    return get_optional_admin_from_token(session_token, request=request)


def get_optional_admin_from_token(
    session_token: str | None,
    *,
    request: Request | None = None,
) -> str | None:
    """Non-FastAPI entrypoint for "validate a session, nullable".

    Used where the token is already pulled out of cookies manually
    (e.g. keep-page admin gating). Fingerprint binding is best-effort:
    if no request is supplied we skip the UA/IP check.
    """
    if not session_token:
        return None
    conn = get_db()
    if request is not None:
        user_agent = request.headers.get("user-agent") or None
        client_ip = get_client_ip(request) or None
    else:
        user_agent = None
        client_ip = None
    return validate_session(
        conn,
        session_token,
        user_agent=user_agent,
        client_ip=client_ip,
    )


def resolve_page_session(
    request: Request,
) -> PageSession:
    """Resolve a session cookie for page routes with fingerprint binding.

    Returns ``(username, conn)`` on a valid session, or a
    ``RedirectResponse("/login", 302)`` otherwise. Every page route uses
    this helper so the UA/IP fingerprint check is always applied.
    """
    token = request.cookies.get("session_token")
    if not token:
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    user_agent = request.headers.get("user-agent") or None
    client_ip = get_client_ip(request) or None
    username = validate_session(
        conn,
        token,
        user_agent=user_agent,
        client_ip=client_ip,
    )
    if username is None:
        return RedirectResponse("/login", status_code=302)
    return username, conn


def is_admin(request: Request) -> bool:
    """Return True when *request* carries a valid admin session cookie.

    Used by page routes that gate admin-only UI affordances without
    rejecting anonymous visitors (keep, kept).
    """
    return (
        get_optional_admin_from_token(request.cookies.get("session_token"), request=request)
        is not None
    )
