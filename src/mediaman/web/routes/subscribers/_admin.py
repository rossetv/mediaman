"""Admin-authenticated subscriber CRUD + manual newsletter send.

All endpoints in this module require a logged-in admin session. The
public, HMAC-token-authenticated unsubscribe flow lives in
:mod:`._unsubscribe`.
"""

from __future__ import annotations

import logging
import re
import sqlite3

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mediaman.core.audit import security_event, security_event_or_raise
from mediaman.core.time import now_iso
from mediaman.db import get_db
from mediaman.services.infra import SafeHTTPError
from mediaman.services.mail.newsletter import NewsletterConfigError
from mediaman.services.rate_limit import get_client_ip
from mediaman.services.rate_limit.instances import (
    NEWSLETTER_LIMITER as _NEWSLETTER_LIMITER,
)
from mediaman.services.rate_limit.instances import (
    SUBSCRIBER_WRITE_LIMITER as _SUBSCRIBER_WRITE_LIMITER,
)
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.repository.subscribers import (
    AddSubscriberOutcome,
    delete_subscriber,
    fetch_active_subscribers_in,
    find_subscriber_by_id,
    list_subscribers,
    try_add_subscriber,
)
from mediaman.web.responses import respond_err, respond_ok


class _SendNewsletterBody(BaseModel):
    """Body shape for POST /api/newsletter/send."""

    recipients: list[str] = []


logger = logging.getLogger(__name__)

router = APIRouter()

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _validate_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


def _resolve_newsletter_recipients(
    conn: sqlite3.Connection, raw_recipients: list[str]
) -> tuple[list[str], int]:
    """Resolve the requested recipient list to deliverable subscriber emails.

    Normalises the raw request list (lower-cased, stripped, non-strings
    dropped), intersects it with the *active* subscribers so the endpoint
    can only ever mail an opted-in address, then filters each candidate
    on CRLF-injection and email-format validity.

    Returns ``(recipients, rejected)`` where ``rejected`` counts candidates
    that survived the subscriber intersection but failed the CRLF/format
    check — that count feeds the ``newsletter.sent`` audit detail.
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
    """Return all subscribers as JSON."""
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
    email: str = Form(...),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Add a new subscriber.

    The SELECT-then-INSERT race is closed inside the repository
    function ``try_add_subscriber`` (BEGIN IMMEDIATE + unique-index
    fallback); the route only translates outcomes into HTTP responses.
    """
    if not _SUBSCRIBER_WRITE_LIMITER.check(username):
        logger.warning("subscriber.add_throttled user=%s", username)
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
        logger.exception("subscriber.add failed user=%s", username)
        return respond_err("internal_error", status=500)

    if outcome is AddSubscriberOutcome.ALREADY_SUBSCRIBED:
        return respond_err("already_subscribed", status=409, message="Email already subscribed")

    logger.info("Subscriber added: %s by %s", email, username)
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
    """Remove a subscriber by ID."""
    if not _SUBSCRIBER_WRITE_LIMITER.check(username):
        logger.warning("subscriber.remove_throttled user=%s", username)
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

    logger.info("Subscriber removed: %s by %s", email, username)
    return respond_ok()


@router.post("/api/newsletter/send")
def api_send_newsletter(
    request: Request,
    body: _SendNewsletterBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Manually send the newsletter to selected recipients."""
    from mediaman.services.mail.newsletter import send_newsletter

    conn = get_db()

    if not _NEWSLETTER_LIMITER.check(admin):
        logger.warning("newsletter.send_throttled user=%s", admin)
        return respond_err(
            "too_many_requests", status=429, message="Newsletter send is rate-limited"
        )

    raw_recipients = body.recipients

    if not isinstance(raw_recipients, list) or not raw_recipients:
        return respond_err("no_recipients", status=400, message="No recipients selected")

    config = request.app.state.config

    if not any(isinstance(r, str) for r in raw_recipients):
        return respond_err("no_valid_recipients", status=400, message="No valid recipients")

    recipients, rejected = _resolve_newsletter_recipients(conn, raw_recipients)

    if not recipients:
        logger.warning(
            "newsletter.send_no_valid_recipients user=%s rejected=%d",
            admin,
            rejected,
        )
        return respond_err(
            "no_valid_recipients", status=400, message="No matching active subscribers"
        )

    try:
        send_newsletter(
            conn=conn,
            secret_key=config.secret_key,
            recipients=recipients,
            mark_notified=False,
        )
        logger.info(
            "Manual newsletter sent to %d recipients by %s (rejected=%d)",
            len(recipients),
            admin,
            rejected,
        )
        security_event(
            conn,
            event="newsletter.sent",
            actor=admin,
            ip=get_client_ip(request),
            detail={"sent_to": len(recipients), "rejected": rejected},
        )
        return respond_ok({"sent_to": len(recipients)})
    except (
        SafeHTTPError,
        requests.RequestException,
        NewsletterConfigError,
        ValueError,
        sqlite3.Error,
    ):
        logger.exception("Manual newsletter send failed")
        return respond_err("send_failed", status=502, message="Newsletter send failed")
