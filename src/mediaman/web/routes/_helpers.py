"""Shared helpers for web route modules.

Kept intentionally small — only helpers that would otherwise be
copy-pasted into three or more route files belong here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import Request

# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------

# Single source of truth for the session-cookie max_age. Duplicating 86400
# across auth.py / users.py / force_password_change.py created a silent
# drift risk where one file was updated and the others weren't.
SESSION_COOKIE_MAX_AGE: int = 86400  # 24 hours


def set_session_cookie(response: JSONResponse, token: str, *, secure: bool) -> None:
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
# Admin gating shorthand
# ---------------------------------------------------------------------------


def is_admin(request: Request) -> bool:
    """Return True when the current request carries a valid admin session.

    Replaces the repeated ``get_optional_admin_from_token(request.cookies.get(
    "session_token"), request=request) is not None`` pattern at four call
    sites (keep.py × 2, kept.py × 2, recommended.py).

    Uses a deferred import to avoid a circular import cycle (middleware
    depends on session, helpers must not depend on middleware at module level).
    """
    from mediaman.auth.middleware import get_optional_admin_from_token

    return (
        get_optional_admin_from_token(request.cookies.get("session_token"), request=request)
        is not None
    )


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def fail(
    code: str,
    message: str,
    *,
    status: int = 400,
    **extra,
) -> JSONResponse:
    """Return a standardised JSON error response.

    All error responses in web routes should go through this helper so
    the envelope shape is consistent:

        {"error": "MACHINE_CODE", "message": "Human-readable text", ...}

    ``code`` is a machine-readable string like ``"not_found"`` or
    ``"invalid_duration"`` — callers can key off this without parsing the
    message. ``message`` is for humans. ``**extra`` lets callers attach
    additional fields (e.g. ``{"issues": [...]}``) without losing the
    standard envelope.

    Usage::

        return fail("not_found", "Media item not found", status=404)
        return fail("rate_limited", "Slow down", status=429)
    """
    body: dict = {"error": code, "message": message}
    body.update(extra)
    return JSONResponse(body, status_code=status)
