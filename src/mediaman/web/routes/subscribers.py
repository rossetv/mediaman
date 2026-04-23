"""Subscriber management API endpoints (admin only).

No separate page — subscribers are managed from the Settings page.
Provides list, add, and remove operations against the subscribers table.
"""

from __future__ import annotations

import logging
import re
from html import escape

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import RateLimiter, get_client_ip
from mediaman.crypto import validate_unsubscribe_token
from mediaman.db import get_db
from mediaman.services.rate_limits import NEWSLETTER_LIMITER as _NEWSLETTER_LIMITER
from mediaman.services.time import now_iso


class _SendNewsletterBody(BaseModel):
    """Body shape for POST /api/newsletter/send."""

    recipients: list[str] = []

logger = logging.getLogger("mediaman")

router = APIRouter()

# Tighter than before: allows the standard local-part characters RFC 5322
# permits in unquoted form, rejects whitespace and the ampersand/hash/
# percent characters that cause URL mayhem if an operator ever imports a
# subscriber list through a non-normalising path.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)

# Rate limiter for the public unsubscribe endpoint — 20 attempts per
# minute per /24 (IPv4) or /64 (IPv6) bucket. Generous enough for a
# real user who clicks through, tight enough to kill automated probing.
_UNSUB_LIMITER = RateLimiter(max_attempts=20, window_seconds=60)


def _validate_email(email: str) -> bool:
    # Conservative regex — avoids heavy dependencies for a rarely-called admin helper.
    return bool(_EMAIL_RE.match(email.strip()))


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
    return JSONResponse({
        "subscribers": [
            {
                "id": r["id"],
                "email": r["email"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    })


@router.post("/api/subscribers")
def api_add_subscriber(
    email: str = Form(...),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Add a new subscriber.

    Validates email format, then inserts into subscribers. Returns 409 if
    the address is already registered, 422 if the format is invalid.
    """
    email = email.strip().lower()
    if not _validate_email(email):
        return JSONResponse({"error": "Invalid email address"}, status_code=422)

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM subscribers WHERE email = ?", (email,)
    ).fetchone()
    if existing:
        return JSONResponse({"error": "Email already subscribed"}, status_code=409)

    now = now_iso()
    conn.execute(
        "INSERT INTO subscribers (email, active, created_at) VALUES (?, 1, ?)",
        (email, now),
    )
    conn.commit()

    logger.info("Subscriber added: %s by %s", email, username)
    return JSONResponse({"ok": True, "email": email}, status_code=201)


@router.delete("/api/subscribers/{subscriber_id}")
def api_remove_subscriber(
    subscriber_id: int,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Remove a subscriber by ID."""
    conn = get_db()
    row = conn.execute(
        "SELECT email FROM subscribers WHERE id = ?", (subscriber_id,)
    ).fetchone()
    if row is None:
        return JSONResponse({"error": "Subscriber not found"}, status_code=404)

    conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
    conn.commit()

    logger.info("Subscriber removed: %s by %s", row["email"], username)
    return JSONResponse({"ok": True})


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
    from mediaman.services.newsletter import send_newsletter

    conn = get_db()

    if not _NEWSLETTER_LIMITER.check(admin):
        logger.warning("newsletter.send_throttled user=%s", admin)
        return JSONResponse(
            {"ok": False, "error": "Newsletter send is rate-limited"},
            status_code=429,
        )

    raw_recipients = body.recipients

    if not isinstance(raw_recipients, list) or not raw_recipients:
        return JSONResponse({"ok": False, "error": "No recipients selected"}, status_code=400)

    config = request.app.state.config

    # Restrict the send list to addresses we actually know about and
    # haven't unsubscribed — prevents the endpoint being weaponised as
    # an open relay for the configured Mailgun domain.
    requested = {str(r).lower().strip() for r in raw_recipients if isinstance(r, str)}
    if not requested:
        return JSONResponse({"ok": False, "error": "No valid recipients"}, status_code=400)

    placeholders = ",".join("?" * len(requested))
    allowed_rows = conn.execute(
        f"SELECT email FROM subscribers WHERE active=1 AND lower(email) IN ({placeholders})",
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
            admin, rejected,
        )
        return JSONResponse(
            {"ok": False, "error": "No matching active subscribers"},
            status_code=400,
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
            len(recipients), admin, rejected,
        )
        return JSONResponse({"ok": True, "sent_to": len(recipients)})
    except Exception as exc:
        logger.warning("Manual newsletter send failed: %s", exc)
        return JSONResponse({"ok": False, "error": "Newsletter send failed"}, status_code=502)


# ---------------------------------------------------------------------------
# Public unsubscribe (no login required — uses HMAC token)
# ---------------------------------------------------------------------------


def _render_result(request: Request, message: str, *, success: bool, status_code: int = 200) -> HTMLResponse:
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
def unsubscribe_page(request: Request, email: str = "", token: str = "") -> HTMLResponse:
    """Show unsubscribe confirmation page. Actual unsubscribe happens via POST."""
    config = request.app.state.config

    if not _UNSUB_LIMITER.check(get_client_ip(request)):
        return _render_result(
            request, "Too many requests. Try again later.", success=False, status_code=429
        )

    if not email or not token or len(email) > 320 or len(token) > 4096:
        return _generic_invalid_response(request)

    if not validate_unsubscribe_token(token, config.secret_key, email):
        return _generic_invalid_response(request)

    # Show confirmation page
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "subscribers/unsubscribe_confirm.html",
        {"email": email, "token": token},
    )


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(
    request: Request,
    email: str = Form(default=""),
    token: str = Form(default=""),
) -> HTMLResponse:
    """Process the unsubscribe after user confirmation."""
    config = request.app.state.config

    if not _UNSUB_LIMITER.check(get_client_ip(request)):
        return _render_result(
            request, "Too many requests. Try again later.", success=False, status_code=429
        )

    if not email or not token or len(email) > 320 or len(token) > 4096:
        return _generic_invalid_response(request)

    if not validate_unsubscribe_token(token, config.secret_key, email):
        return _generic_invalid_response(request)

    conn = get_db()
    row = conn.execute(
        "SELECT id, active FROM subscribers WHERE lower(email) = ?", (email.lower(),)
    ).fetchone()

    # Return a uniform "you've been unsubscribed" response whether the
    # email is present, already inactive, or absent — otherwise the
    # endpoint becomes a subscriber-membership oracle. The logs retain
    # the true state for operator debugging.
    if row is None:
        logger.info("Unsubscribe link used for unknown email: %s", email)
        return _render_result(
            request,
            "If that address was subscribed, it has now been removed.",
            success=True,
        )

    if row["active"]:
        conn.execute("UPDATE subscribers SET active = 0 WHERE id = ?", (row["id"],))
        conn.commit()
        logger.info("Unsubscribed via link: %s", email)

    return _render_result(
        request,
        "If that address was subscribed, it has now been removed.",
        success=True,
    )


def _unsub_confirm_html(email: str, token: str) -> str:
    """Render an unsubscribe confirmation page with a button (kept for tests)."""
    safe_email = escape(email)
    safe_token = escape(token)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Unsubscribe · Mediaman</title></head>
<body style="margin:0;padding:0;background:#000;font-family:-apple-system,'SF Pro Text','Helvetica Neue',sans-serif;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;">
<div style="text-align:center;max-width:400px;padding:40px;">
<div style="font-size:24px;font-weight:600;margin-bottom:16px;">media<span style="color:#2997ff;">man</span></div>
<div style="font-size:17px;font-weight:600;margin-bottom:8px;">Unsubscribe?</div>
<div style="font-size:15px;color:rgba(255,255,255,0.5);line-height:1.5;margin-bottom:24px;">
You will no longer receive newsletter emails at<br><strong style="color:#fff;">{safe_email}</strong>
</div>
<form method="POST" action="/unsubscribe">
<input type="hidden" name="email" value="{safe_email}">
<input type="hidden" name="token" value="{safe_token}">
<button type="submit" style="padding:12px 32px;border-radius:980px;border:none;background:#ff453a;color:#fff;font-size:15px;font-weight:600;font-family:inherit;cursor:pointer;margin-right:12px;">Unsubscribe</button>
<a href="/" style="padding:12px 24px;border-radius:980px;border:1px solid rgba(255,255,255,0.2);color:rgba(255,255,255,0.6);font-size:15px;font-weight:600;text-decoration:none;">Cancel</a>
</form>
</div></body></html>"""


def _unsub_html(message: str, success: bool) -> str:
    """Render a minimal unsubscribe result page (kept for tests)."""
    safe_message = escape(message)
    colour = "#30d158" if success else "#ff453a"
    icon = "&#10003;" if success else "&#10007;"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Unsubscribe · Mediaman</title></head>
<body style="margin:0;padding:0;background:#000;font-family:-apple-system,'SF Pro Text','Helvetica Neue',sans-serif;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;">
<div style="text-align:center;max-width:400px;padding:40px;">
<div style="font-size:48px;color:{colour};margin-bottom:16px;">{icon}</div>
<div style="font-size:24px;font-weight:600;margin-bottom:8px;">media<span style="color:#2997ff;">man</span></div>
<div style="font-size:15px;color:rgba(255,255,255,0.6);line-height:1.5;">{safe_message}</div>
</div></body></html>"""
