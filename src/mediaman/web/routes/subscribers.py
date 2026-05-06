"""Subscriber management API endpoints (admin only).

No separate page — subscribers are managed from the Settings page.
Provides list, add, and remove operations against the subscribers table.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from mediaman.audit import security_event
from mediaman.core.time import now_iso
from mediaman.crypto import validate_unsubscribe_token
from mediaman.db import get_db
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.services.rate_limit.instances import (
    NEWSLETTER_LIMITER as _NEWSLETTER_LIMITER,
)
from mediaman.services.rate_limit.instances import (
    SUBSCRIBER_WRITE_LIMITER as _SUBSCRIBER_WRITE_LIMITER,
)
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.responses import respond_err, respond_ok


class _SendNewsletterBody(BaseModel):
    """Body shape for POST /api/newsletter/send."""

    recipients: list[str] = []


logger = logging.getLogger("mediaman")

router = APIRouter()

#: Uniform confirmation message returned for both the "present and active",
#: "already inactive", and "not found" cases, so the endpoint cannot be used
#: as a subscriber-membership oracle.
_UNSUB_CONFIRMATION_MSG = "If that address was subscribed, it has now been removed."

# Tighter than before: allows the standard local-part characters RFC 5322
# permits in unquoted form, rejects whitespace and the ampersand/hash/
# percent characters that cause URL mayhem if an operator ever imports a
# subscriber list through a non-normalising path.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Rate limiter for the public unsubscribe endpoint — 20 attempts per
# minute per /24 (IPv4) or /64 (IPv6) bucket. Generous enough for a
# real user who clicks through, tight enough to kill automated probing.
_UNSUB_LIMITER = RateLimiter(max_attempts=20, window_seconds=60)


def _validate_email(email: str) -> bool:
    # Conservative regex — avoids heavy dependencies for a rarely-called admin helper.
    return bool(_EMAIL_RE.match(email.strip()))


def _mask_email_log(email: str) -> str:
    """Return a masked email for log output (first char of local-part + domain + length).

    Avoids logging PII in plaintext while still giving operators enough
    context to triage delivery issues without logging PII in plaintext.
    """
    try:
        local, domain = email.split("@", 1)
    except ValueError:
        return f"(len={len(email)})"
    first = local[0] if local else "?"
    return f"{first}...@{domain} (len={len(email)})"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/subscribers")
def api_list_subscribers(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all subscribers as JSON."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, email, active, created_at FROM subscribers ORDER BY created_at ASC"
    ).fetchall()
    return JSONResponse(
        {
            "subscribers": [
                {
                    "id": r["id"],
                    "email": r["email"],
                    "active": bool(r["active"]),
                    "created_at": r["created_at"],
                }
                for r in rows
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

    Validates email format, then inserts into subscribers. Returns 409 if
    the address is already registered, 422 if the format is invalid.

    The SELECT-then-INSERT path is wrapped in ``BEGIN IMMEDIATE`` so two
    concurrent admin sessions adding the same email cannot both pass the
    SELECT and then race on the INSERT — the loser hits the unique
    constraint and is converted to a clean 409 instead of bubbling a 500
    out of the IntegrityError.
    """
    if not _SUBSCRIBER_WRITE_LIMITER.check(username):
        logger.warning("subscriber.add_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many subscriber changes — slow down"
        )
    email = email.strip().lower()
    if not _validate_email(email):
        return respond_err("invalid_email", status=422, message="Invalid email address")

    conn = get_db()
    now = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT id FROM subscribers WHERE email = ?", (email,)
            ).fetchone()
            if existing:
                conn.execute("ROLLBACK")
                return respond_err(
                    "already_subscribed", status=409, message="Email already subscribed"
                )
            try:
                conn.execute(
                    "INSERT INTO subscribers (email, active, created_at) VALUES (?, 1, ?)",
                    (email, now),
                )
            except sqlite3.IntegrityError:
                # Concurrent admin landed first — convert to a clean
                # 409. Without this, both racers passed the SELECT,
                # one INSERT succeeded, the other returned a 500.
                conn.execute("ROLLBACK")
                return respond_err(
                    "already_subscribed", status=409, message="Email already subscribed"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except Exception:
        logger.exception("subscriber.add failed user=%s", username)
        return respond_err("internal_error", status=500)

    masked = _mask_email_log(email)
    logger.info("Subscriber added: %s by %s", masked, username)
    security_event(
        conn,
        event="subscriber.added",
        actor=username,
        ip=get_client_ip(request),
        detail={"email": masked},
    )
    return respond_ok({"email": email}, status=201)


@router.delete("/api/subscribers/{subscriber_id}")
def api_remove_subscriber(
    request: Request,
    subscriber_id: int,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Remove a subscriber by ID.

    Rate-limited per admin to bound abuse via a leaked session cookie,
    and writes a security_event audit row so a compromised account
    cannot silently churn the subscriber list.
    """
    if not _SUBSCRIBER_WRITE_LIMITER.check(username):
        logger.warning("subscriber.remove_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many subscriber changes — slow down"
        )
    conn = get_db()
    row = conn.execute("SELECT email FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
    if row is None:
        return respond_err("not_found", status=404, message="Subscriber not found")

    conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
    conn.commit()

    masked = _mask_email_log(row["email"] or "")
    logger.info("Subscriber removed: %s by %s", masked, username)
    security_event(
        conn,
        event="subscriber.removed",
        actor=username,
        ip=get_client_ip(request),
        detail={"id": subscriber_id, "email": masked},
    )
    return respond_ok()


@router.post("/api/newsletter/send")
def api_send_newsletter(
    request: Request,
    body: _SendNewsletterBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Manually send the newsletter to selected recipients.

    Expects JSON body: ``{"recipients": ["email@example.com", ...]}``.
    The per-admin rate limiter is the safeguard against the endpoint
    being abused to spam the subscriber list.

    Sends the current newsletter (all active scheduled items) without marking
    them as notified, so the regular scan newsletter still sends normally.
    """
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

    # Restrict the send list to addresses we actually know about and
    # haven't unsubscribed — prevents the endpoint being weaponised as
    # an open relay for the configured Mailgun domain.
    requested = {str(r).lower().strip() for r in raw_recipients if isinstance(r, str)}
    if not requested:
        return respond_err("no_valid_recipients", status=400, message="No valid recipients")

    placeholders = ",".join("?" * len(requested))
    # ``subscribers.email`` is normalised to lowercase on write (M14)
    # and the column carries a UNIQUE INDEX with COLLATE NOCASE — no
    # need for a function call here. Wrapping the column in lower()
    # forced a full table scan and defeated the index, which on a
    # large subscriber list became a measurable hot path.
    allowed_rows = conn.execute(
        f"SELECT email FROM subscribers WHERE active=1 AND email IN ({placeholders})",
        tuple(requested),
    ).fetchall()

    # Re-validate every recipient pulled from the DB before it hits the
    # Mailgun SMTP headers. A malicious subscriber row containing CR/LF
    # would let an attacker inject ``Bcc:`` or similar headers through
    # the templated ``To:`` field. The add-subscriber path already runs
    # ``_validate_email``, but a DB compromise or a historic row written
    # before this check existed could still carry a CRLF payload.
    recipients: list[str] = []
    rejected = 0
    for r in allowed_rows:
        candidate = r["email"] or ""
        if "\r" in candidate or "\n" in candidate:
            rejected += 1
            continue
        if not _validate_email(candidate):
            rejected += 1
            continue
        recipients.append(candidate)

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
    except Exception as exc:
        logger.warning("Manual newsletter send failed: %s", exc)
        return respond_err("send_failed", status=502, message="Newsletter send failed")


# ---------------------------------------------------------------------------
# Public unsubscribe (no login required — uses HMAC token)
# ---------------------------------------------------------------------------


def _render_result(
    request: Request, message: str, *, success: bool, status_code: int = 200
) -> HTMLResponse:
    """Render the unsubscribe result page via the Jinja template."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "subscribers/unsubscribe_result.html",
        {"message": message, "success": success},
        status_code=status_code,
    )


def _generic_invalid_response(request: Request) -> HTMLResponse:
    """Return a uniform "invalid link" response so we don't leak
    whether an email is subscribed, whether a token was syntactically
    well-formed but wrong-signed, or whether it has expired."""
    return _render_result(request, "This unsubscribe link is no longer valid.", success=False)


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str = "", email: str = "") -> HTMLResponse:
    """Show unsubscribe confirmation page.

    Accepts ``?token=...`` only — the email address is derived from the
    validated token payload — the email is not accepted from query parameters to avoid leaking PII in server logs.  The legacy ``email=`` parameter
    is accepted but ignored; the token is authoritative.
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

    # Derive the email address from the validated token — never from the URL.
    email_from_token = payload.get("email", "").lower()
    if not email_from_token:
        return _generic_invalid_response(request)

    # Show confirmation page
    templates = request.app.state.templates
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
    use.  The exemption is opt-in via the explicit
    ``_CSRF_EXEMPT_ROUTES`` allowlist in :mod:`mediaman.web` — adding a
    sibling ``POST /unsubscribe/...`` will NOT silently inherit the
    exemption.

    The email is derived from the validated token, not from form input
    The ``email`` form field is accepted for backwards
    compatibility with the confirmation template but is not used for
    lookup or authorisation.
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

    # Always use the email from the token — never trust form/URL input.
    email_from_token = payload.get("email", "").lower()
    if not email_from_token:
        return _generic_invalid_response(request)

    conn = get_db()
    # ``subscribers.email`` is normalised to lowercase on write (M14),
    # so we look up directly by the column. Wrapping it in lower()
    # forced a full table scan — the schema's UNIQUE INDEX with
    # COLLATE NOCASE is sufficient.
    row = conn.execute(
        "SELECT id, active FROM subscribers WHERE email = ?", (email_from_token,)
    ).fetchone()

    # Return a uniform "you've been unsubscribed" response whether the
    # email is present, already inactive, or absent — otherwise the
    # endpoint becomes a subscriber-membership oracle. The logs retain
    # the true state for operator debugging with masked addresses.
    if row is None:
        logger.info(
            "Unsubscribe link used for unknown email: %s", _mask_email_log(email_from_token)
        )
        return _render_result(
            request,
            _UNSUB_CONFIRMATION_MSG,
            success=True,
        )

    if row["active"]:
        conn.execute("UPDATE subscribers SET active = 0 WHERE id = ?", (row["id"],))
        conn.commit()
        logger.info("Unsubscribed via link: %s", _mask_email_log(email_from_token))

    return _render_result(
        request,
        _UNSUB_CONFIRMATION_MSG,
        success=True,
    )
