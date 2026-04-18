"""Subscriber management API endpoints (admin only).

No separate page — subscribers are managed from the Settings page.
Provides list, add, and remove operations against the subscribers table.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html import escape

import hashlib
import hmac

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from mediaman.auth.middleware import get_current_admin
from mediaman.db import get_db


class _SendNewsletterBody(BaseModel):
    """Body shape for POST /api/newsletter/send."""

    recipients: list[str] = []

logger = logging.getLogger("mediaman")

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(email: str) -> bool:
    """Return True if email looks syntactically valid."""
    return bool(_EMAIL_RE.match(email.strip()))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/subscribers")
def api_list_subscribers(username: str = Depends(get_current_admin)):
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
):
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

    now = datetime.now(timezone.utc).isoformat()
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
):
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
):
    """Manually send the newsletter to selected recipients.

    Expects JSON body: ``{"recipients": ["email@example.com", ...]}``.
    Sends the current newsletter (all active scheduled items) without marking
    them as notified, so the regular scan newsletter still sends normally.
    """
    from mediaman.services.newsletter import send_newsletter

    raw_recipients = body.recipients

    if not isinstance(raw_recipients, list) or not raw_recipients:
        return JSONResponse({"ok": False, "error": "No recipients selected"}, status_code=400)

    conn = get_db()
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
    recipients = [r["email"] for r in allowed_rows]
    if not recipients:
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
        logger.info("Manual newsletter sent to %s by %s", recipients, admin)
        return JSONResponse({"ok": True, "sent_to": len(recipients)})
    except Exception as exc:
        logger.warning("Manual newsletter send failed: %s", exc)
        return JSONResponse({"ok": False, "error": "Newsletter send failed"}, status_code=502)


# ---------------------------------------------------------------------------
# Public unsubscribe (no login required — uses HMAC token)
# ---------------------------------------------------------------------------

def generate_unsubscribe_token(email: str, secret_key: str) -> str:
    """Generate an HMAC-SHA256 token for unsubscribe verification."""
    return hmac.new(
        secret_key.encode(), email.lower().encode(), hashlib.sha256
    ).hexdigest()[:32]


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(request: Request, email: str = "", token: str = ""):
    """Show unsubscribe confirmation page. Actual unsubscribe happens via POST."""
    from mediaman.config import load_config
    config = load_config()

    if not email or not token:
        return HTMLResponse(_unsub_html("Invalid unsubscribe link.", success=False))

    expected = generate_unsubscribe_token(email, config.secret_key)
    if not hmac.compare_digest(token, expected):
        return HTMLResponse(_unsub_html("Invalid unsubscribe link.", success=False))

    # Show confirmation page
    return HTMLResponse(_unsub_confirm_html(email, token))


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(
    request: Request,
    email: str = Form(default=""),
    token: str = Form(default=""),
):
    """Process the unsubscribe after user confirmation."""
    from mediaman.config import load_config
    config = load_config()

    if not email or not token:
        return HTMLResponse(_unsub_html("Invalid unsubscribe link.", success=False))

    expected = generate_unsubscribe_token(email, config.secret_key)
    if not hmac.compare_digest(token, expected):
        return HTMLResponse(_unsub_html("Invalid unsubscribe link.", success=False))

    conn = get_db()
    row = conn.execute(
        "SELECT id, active FROM subscribers WHERE email = ?", (email.lower(),)
    ).fetchone()

    if row is None:
        return HTMLResponse(_unsub_html("Email not found in subscriber list.", success=False))

    if not row["active"]:
        return HTMLResponse(_unsub_html(f"{email} is already unsubscribed.", success=True))

    conn.execute("UPDATE subscribers SET active = 0 WHERE id = ?", (row["id"],))
    conn.commit()
    logger.info("Unsubscribed via link: %s", email)

    return HTMLResponse(_unsub_html(f"{email} has been unsubscribed. You will no longer receive newsletters.", success=True))


def _unsub_confirm_html(email: str, token: str) -> str:
    """Render an unsubscribe confirmation page with a button."""
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
    """Render a minimal unsubscribe result page."""
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
