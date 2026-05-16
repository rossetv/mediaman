"""Public unsubscribe flow.

The endpoints in this module are CSRF-exempt: they are
HMAC-token-authenticated (the token rides in the URL/form body) and get
submitted from email clients where the browser's ``Origin`` is whichever
webmail host the recipient happens to use. The per-IP rate limiter
defends against link-fuzzing.
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mediaman.crypto import validate_unsubscribe_token
from mediaman.db import get_db
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.repository.subscribers import (
    deactivate_subscriber,
    find_subscriber_status_by_email,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: Uniform confirmation message returned for both the "present and active",
#: "already inactive", and "not found" cases, so the endpoint cannot be used
#: as a subscriber-membership oracle.
_UNSUB_CONFIRMATION_MSG = "If that address was subscribed, it has now been removed."

# Rate-limiter is process-scoped: per-IP attempt counters must persist across
# requests in the same worker to enforce the unsubscribe rate window correctly.
_UNSUB_LIMITER = RateLimiter(max_attempts=20, window_seconds=60)


def _render_result(
    request: Request, message: str, *, success: bool, status_code: int = 200
) -> HTMLResponse:
    """Render the unsubscribe result page via the Jinja template."""
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "subscribers/unsubscribe_result.html",
        {"message": message, "success": success},
        status_code=status_code,
    )


def _generic_invalid_response(request: Request) -> HTMLResponse:
    """Return a uniform invalid link response."""
    return _render_result(request, "This unsubscribe link is no longer valid.", success=False)


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str = "", email: str = "") -> HTMLResponse:
    """Show unsubscribe confirmation page.

    Accepts token= only -- the email address is derived from the
    validated token payload.
    """
    config = request.app.state.config

    if not _UNSUB_LIMITER.check(get_client_ip(request)):
        return _render_result(
            request, "Too many requests. Try again later.", success=False, status_code=429
        )

    if not token or len(token) > 4096:
        return _generic_invalid_response(request)

    payload = validate_unsubscribe_token(token, config.secret_key)
    if payload is None:
        return _generic_invalid_response(request)

    email_from_token = payload.get("email", "").lower()
    if not email_from_token:
        return _generic_invalid_response(request)

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "subscribers/unsubscribe_confirm.html",
        {"email": email_from_token, "token": token},
    )


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(
    request: Request,
    token: str = Form(default=""),
    email: str = Form(default=""),
) -> HTMLResponse:
    """Process the unsubscribe after user confirmation.

    CSRF-exempt: this route is HMAC-token-authenticated (the token rides
    in the form body) and gets submitted from email clients where the
    browser's Origin is whichever webmail host the recipient happens to
    use.
    """
    config = request.app.state.config

    if not _UNSUB_LIMITER.check(get_client_ip(request)):
        return _render_result(
            request, "Too many requests. Try again later.", success=False, status_code=429
        )

    if not token or len(token) > 4096:
        return _generic_invalid_response(request)

    payload = validate_unsubscribe_token(token, config.secret_key)
    if payload is None:
        return _generic_invalid_response(request)

    email_from_token = payload.get("email", "").lower()
    if not email_from_token:
        return _generic_invalid_response(request)

    conn = get_db()
    sub_status = find_subscriber_status_by_email(conn, email_from_token)

    if sub_status is None:
        logger.info("Unsubscribe link used for unknown email: %s", email_from_token)
        return _render_result(
            request,
            _UNSUB_CONFIRMATION_MSG,
            success=True,
        )

    sub_id, is_active = sub_status
    if is_active:
        deactivate_subscriber(conn, sub_id)
        conn.commit()
        logger.info("Unsubscribed via link: %s", email_from_token)

    return _render_result(
        request,
        _UNSUB_CONFIRMATION_MSG,
        success=True,
    )
