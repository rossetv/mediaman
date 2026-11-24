"""Subscriber management API endpoints (admin only). There is no separate page; subscribers are
managed from the Settings page.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import cast

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from mediaman.core.audit import security_event, security_event_or_raise
from mediaman.core.time import now_iso
from mediaman.crypto import validate_unsubscribe_token
from mediaman.db import get_db
from mediaman.services.infra import SafeHTTPError
from mediaman.services.mail.newsletter import NewsletterConfigError
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.services.rate_limit.instances import (
    NEWSLETTER_LIMITER as _NEWSLETTER_LIMITER,
)
from mediaman.services.rate_limit.instances import (
    SUBSCRIBER_WRITE_LIMITER as _SUBSCRIBER_WRITE_LIMITER,
)
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.repository.subscribers import (
    AddSubscriberOutcome,
    deactivate_subscriber,
    delete_subscriber,
    fetch_active_subscribers_in,
    find_subscriber_by_id,
    find_subscriber_status_by_email,
    list_subscribers,
    try_add_subscriber,
)
from mediaman.web.responses import respond_err, respond_ok


class _SendNewsletterBody(BaseModel):
    recipients: list[str] = []


logger = logging.getLogger(__name__)

router = APIRouter()

#: Uniform confirmation message returned for both the "present and active",
#: "already inactive", and "not found" cases, so the endpoint cannot be used
#: as a subscriber-membership oracle.
_UNSUB_CONFIRMATION_MSG = "If that address was subscribed, it has now been removed."

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Rate-limiter is process-scoped: per-IP attempt counters must persist across
# requests in the same worker to enforce the unsubscribe rate window correctly.
_UNSUB_LIMITER = RateLimiter(max_attempts=20, window_seconds=60)


def _validate_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


def _resolve_newsletter_recipients(
    conn: sqlite3.Connection, raw_recipients: list[str]
) -> tuple[list[str], int]:
    """Resolve the requested recipient list to deliverable subscriber emails.

    The raw list is normalised, intersected with the active subscribers so the endpoint can
    only mail an opted-in address, then filtered for CRLF injection and email format.
    Returns (recipients, rejected); rejected counts candidates that passed the intersection
    but failed that check, and feeds the newsletter.sent audit detail.
    """
    requested = {str(r).lower().strip() for r in raw_recipients if isinstance(r, str)}
    if not requested:
        return [], 0

    allowed_emails = fetch_active_subscribers_in(conn, requested)

    recipients: list[str] = []
    rejected = 0
    for candidate in allowed_emails:
        if "\r" in candidate or "\n" in candidate:
            rejected += 1
            continue
        if not _validate_email(candidate):
            rejected += 1
            continue
        recipients.append(candidate)
    return recipients, rejected


@router.get("/api/subscribers")
def api_list_subscribers(username: str = Depends(get_current_admin)) -> JSONResponse:
    conn = get_db()
    subs = list_subscribers(conn)
    return JSONResponse(
        {
            "subscribers": [
                {
                    "id": s.id,
                    "email": s.email,
                    "active": s.active,
                    "created_at": s.created_at,
                }
                for s in subs
            ]
        }
    )


@router.post("/api/subscribers")
def api_add_subscriber(
    request: Request,
    email: str = Form(default=""),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Add a new subscriber. The SELECT-then-INSERT race is closed in the repository's
    try_add_subscriber (BEGIN IMMEDIATE plus unique-index fallback); the route only maps
    outcomes to HTTP responses.
    """
    if not _SUBSCRIBER_WRITE_LIMITER.check(username):
        logger.warning("subscriber.add_throttled user=%s", _sanitise_log_field(username))
        return respond_err(
            "too_many_requests", status=429, message="Too many subscriber changes -- slow down"
        )
    email = email.strip().lower()
    if not _validate_email(email):
        return respond_err("invalid_email", status=422, message="Invalid email address")

    conn = get_db()
    now = now_iso()
    try:
        outcome = try_add_subscriber(conn, email=email, now=now)
    except sqlite3.Error:
        logger.exception("subscriber.add failed user=%s", _sanitise_log_field(username))
        return respond_err("internal_error", status=500, message="Couldn't add subscriber.")

    if outcome is AddSubscriberOutcome.ALREADY_SUBSCRIBED:
        return respond_err("already_subscribed", status=409, message="Email already subscribed")

    logger.info("Subscriber added: %s by %s", email, _sanitise_log_field(username))
    security_event(
        conn,
        event="subscriber.added",
        actor=username,
        ip=get_client_ip(request),
        detail={"email": email},
    )
    return respond_ok({"email": email}, status=201)


@router.delete("/api/subscribers/{subscriber_id}")
def api_remove_subscriber(
    request: Request,
    subscriber_id: int,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    if not _SUBSCRIBER_WRITE_LIMITER.check(username):
        logger.warning("subscriber.remove_throttled user=%s", _sanitise_log_field(username))
        return respond_err(
            "too_many_requests", status=429, message="Too many subscriber changes -- slow down"
        )
    conn = get_db()
    email = find_subscriber_by_id(conn, subscriber_id)
    if email is None:
        return respond_err("not_found", status=404, message="Subscriber not found")

    with conn:
        delete_subscriber(conn, subscriber_id)
        security_event_or_raise(
            conn,
            event="subscriber.removed",
            actor=username,
            ip=get_client_ip(request),
            detail={"id": subscriber_id, "email": email},
        )

    logger.info("Subscriber removed: %s by %s", email, _sanitise_log_field(username))
    return respond_ok()


@router.post("/api/newsletter/send")
def api_send_newsletter(
    request: Request,
    body: _SendNewsletterBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    from mediaman.services.mail.newsletter import send_newsletter

    conn = get_db()

    raw_recipients = body.recipients

    if not raw_recipients:
        return respond_err("no_recipients", status=400, message="No recipients selected")

    config = request.app.state.config

    recipients, rejected = _resolve_newsletter_recipients(conn, raw_recipients)

    if not recipients:
        logger.warning(
            "newsletter.send_no_valid_recipients user=%s rejected=%d",
            _sanitise_log_field(admin),
            rejected,
        )
        return respond_err(
            "no_valid_recipients", status=400, message="No matching active subscribers"
        )

    if not _NEWSLETTER_LIMITER.check(admin):
        logger.warning("newsletter.send_throttled user=%s", _sanitise_log_field(admin))
        return respond_err(
            "too_many_requests", status=429, message="Newsletter send is rate-limited"
        )

    try:
        result = send_newsletter(
            conn=conn,
            secret_key=config.secret_key,
            recipients=recipients,
            mark_notified=False,
        )
    except (
        SafeHTTPError,
        requests.RequestException,
        NewsletterConfigError,
        ValueError,
        sqlite3.Error,
    ):
        logger.exception("Manual newsletter send failed")
        return respond_err("send_failed", status=502, message="Newsletter send failed")

    if not result.configured:
        return respond_err(
            "mailgun_not_configured", status=400, message="Email sending is not set up"
        )
    if result.attempted == 0:
        return respond_ok({"sent_to": 0, "message": "Nothing to send right now"})
    if result.sent == 0:
        logger.error("Manual newsletter send failed for all %d recipients", result.attempted)
        return respond_err("send_failed", status=502, message="Newsletter send failed")

    logger.info(
        "Manual newsletter sent to %d of %d recipients by %s (rejected=%d)",
        result.sent,
        result.attempted,
        _sanitise_log_field(admin),
        rejected,
    )
    security_event(
        conn,
        event="newsletter.sent",
        actor=admin,
        ip=get_client_ip(request),
        detail={"sent_to": result.sent, "rejected": rejected},
    )
    reply: dict[str, object] = {"sent_to": result.sent}
    if result.sent < result.attempted:
        reply["failed"] = result.attempted - result.sent
    return respond_ok(reply)


def _render_result(
    request: Request, message: str, *, success: bool, status_code: int = 200
) -> HTMLResponse:
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "subscribers/unsubscribe_result.html",
        {"message": message, "success": success},
        status_code=status_code,
    )


def _generic_invalid_response(request: Request) -> HTMLResponse:
    return _render_result(request, "This unsubscribe link is no longer valid.", success=False)


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str = "", email: str = "") -> HTMLResponse:
    """Show the unsubscribe confirmation page. Accepts token= only; the email address is
    derived from the validated token payload.
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
    """Process the unsubscribe after confirmation. CSRF-exempt: the route is
    HMAC-token-authenticated (the token rides in the form body) and submitted from email
    clients whose Origin is whichever webmail host the recipient uses.
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
        with conn:
            deactivate_subscriber(conn, sub_id)
            security_event_or_raise(
                conn,
                event="subscriber.opted_out",
                actor=email_from_token,
                ip=get_client_ip(request),
                detail={"id": sub_id},
            )
        logger.info("Unsubscribed via link: %s", email_from_token)

    return _render_result(
        request,
        _UNSUB_CONFIRMATION_MSG,
        success=True,
    )
