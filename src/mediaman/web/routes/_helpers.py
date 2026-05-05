"""Shared helpers for web route modules.

Kept intentionally small — only helpers that would otherwise be
copy-pasted into three or more route files belong here.
"""

from __future__ import annotations

from fastapi import Request
from starlette.responses import Response

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
# Admin gating shorthand
# ---------------------------------------------------------------------------


def is_admin(request: Request) -> bool:
    """Return True when the current request carries a valid admin session.

    Replaces the repeated ``get_optional_admin_from_token(request.cookies.get(
    "session_token"), request=request) is not None`` pattern at four call
    sites (keep.py × 2, kept.py × 2, recommended.py).

    The import is deferred so that test patches on
    ``mediaman.auth.middleware.get_optional_admin_from_token`` take effect
    at call time rather than at module-load time.
    """
    from mediaman.web.auth.middleware import get_optional_admin_from_token

    return (
        get_optional_admin_from_token(request.cookies.get("session_token"), request=request)
        is not None
    )


# `fail()` lived here as an attempt at a unified error envelope but was
# never adopted by any route — handlers continued to construct ad-hoc
# JSONResponse bodies. Domain 12 flagged the dead code; rather than
# touch ~27 call sites plus the tests that assert their existing shapes
# (a high-risk change for cosmetic gain), the helper was deleted along
# with its test class. If a future refactor wants a unified envelope,
# pick one of the existing shapes (``{"ok": False, "error": "..."}``
# is the most common) and codemod every call site rather than adding
# another competing helper.
