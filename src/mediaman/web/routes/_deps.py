"""Single source of truth for cross-route FastAPI dependencies.

Every route file that previously inlined session resolution, config
access, or admin gating pulls from here instead.  Centralising these
removes ~20 repetitive ``config = request.app.state.config`` lines and
the ``resolve_page_session`` + isinstance dance from page handlers.

Dependency summary
------------------
``get_config``
    Returns the app-level :class:`~mediaman.config.Config` object.
    Replaces ``config = request.app.state.config``.

``get_page_session``
    For page (HTML) route handlers.  Returns ``(username, conn)`` on a
    valid session, or a :class:`~starlette.responses.RedirectResponse`
    to ``/login`` otherwise.  Wraps :func:`~mediaman.auth.middleware.resolve_page_session`.

``require_admin``
    For API route handlers.  Returns the authenticated admin username or
    raises :class:`~fastapi.HTTPException` 403.

``require_admin_page``
    For page route handlers.  Returns the authenticated admin username
    or a :class:`~starlette.responses.RedirectResponse` to ``/login``.
"""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException, Request
from starlette.responses import RedirectResponse

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.config import Config


def get_config(request: Request) -> Config:
    """Return the app-level :class:`~mediaman.config.Config` from request state.

    Intended for use as a FastAPI dependency::

        @router.get("/api/something")
        def my_endpoint(config: Config = Depends(get_config)) -> ...:
            ...

    Args:
        request: The current FastAPI request — injected automatically.

    Returns:
        The :class:`~mediaman.config.Config` instance stored on
        ``request.app.state.config`` at startup.
    """
    return request.app.state.config


def get_page_session(
    request: Request,
) -> tuple[str, sqlite3.Connection] | RedirectResponse:
    """Resolve a page session for HTML route handlers.

    Wraps :func:`~mediaman.auth.middleware.resolve_page_session` for use
    as a FastAPI dependency.  Returns ``(username, conn)`` on success or
    a ``RedirectResponse("/login", 302)`` when the session is missing or
    invalid.

    Because FastAPI cannot automatically short-circuit a handler when a
    dependency returns a :class:`~starlette.responses.RedirectResponse`
    (as opposed to raising), page handlers that use this dependency must
    still check the return type::

        @router.get("/page")
        def my_page(session = Depends(get_page_session)) -> Response:
            if isinstance(session, RedirectResponse):
                return session
            username, conn = session
            ...

    Args:
        request: The current FastAPI request — injected automatically.

    Returns:
        ``(username, conn)`` on a valid session, or a
        :class:`~starlette.responses.RedirectResponse` to ``/login``.
    """
    return resolve_page_session(request)


def require_admin(request: Request) -> str:
    """Authenticate the request and return the admin username.

    Raises :class:`~fastapi.HTTPException` 403 (not 401) when the
    session is absent or invalid — page-less API routes should present
    an opaque auth failure rather than leaking session state via the
    status code.  Use :func:`~mediaman.auth.middleware.get_current_admin`
    (which raises 401) for the standard ``Depends`` pattern where the
    OpenAPI schema should reflect an unauthenticated error.

    Args:
        request: The current FastAPI request — injected automatically.

    Returns:
        The authenticated admin username.

    Raises:
        HTTPException: 403 when the session is absent or invalid.
    """
    try:
        return get_current_admin(request)
    except HTTPException as exc:
        raise HTTPException(status_code=403, detail="Forbidden") from exc


def require_admin_page(request: Request) -> str | RedirectResponse:
    """Authenticate the request for a page (HTML) route.

    Returns the admin username on success or a
    :class:`~starlette.responses.RedirectResponse` to ``/login`` when the
    session is absent or invalid.  Page handlers must check the return
    type, identical to the :func:`get_page_session` pattern::

        @router.get("/admin-only-page")
        def admin_page(admin = Depends(require_admin_page)) -> Response:
            if isinstance(admin, RedirectResponse):
                return admin
            ...

    Args:
        request: The current FastAPI request — injected automatically.

    Returns:
        The authenticated admin username, or a redirect to ``/login``.
    """
    try:
        return get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)
