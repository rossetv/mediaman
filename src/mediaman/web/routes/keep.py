"""Public keep page: token-authenticated snooze for scheduled deletions.

The "forever" keep lives on a separate authenticated, CSRF-protected endpoint (POST
/api/keep/{token}/forever) so the privileged action needs an admin session rather than the
CSRF-exempt public route.

The guarded UPDATE requires action='scheduled_deletion', delete_status='pending',
token_used=0 and execute_at >= now(), so expired or processed rows cannot be snoozed. Only
the SHA-256 token hash is stored, and lookups use token_hash, so the plaintext never touches
the database. Verification, parsing and the guarded UPDATE live in
mediaman.services.scheduled_actions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import cast

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc
from mediaman.crypto import sign_poster_url
from mediaman.db import get_db
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.services.scheduled_actions import (
    VerifiedKeepAction,
    apply_keep_forever,
    apply_keep_snooze,
    format_added_display,
    is_keep_token_consumed,
    is_pending_unexpired,
    lookup_verified_action,
    mark_token_consumed,
    parse_execute_at,
    token_hash,
)
from mediaman.services.scheduled_actions.days_left import days_left as _days_left
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.auth.middleware import is_admin as _is_admin
from mediaman.web.models import VALID_KEEP_DURATIONS
from mediaman.web.responses import respond_err, respond_ok

router = APIRouter()

# Separate rate limiters for GET and POST so an automated prober
# exhausting the GET budget cannot block a legitimate POST (snooze)
# and vice-versa.  Both are bucketed by /24 (v4) / /64 (v6) prefix.
_KEEP_GET_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)
_KEEP_POST_LIMITER = RateLimiter(max_attempts=10, window_seconds=60)


__all__ = [
    "router",
]


def _consume_keep_token_and_apply(
    conn: sqlite3.Connection,
    *,
    token: str,
    verified: VerifiedKeepAction,
    now: datetime,
    apply: Callable[[], int],
    audit_detail: str,
    audit_action: str = "snoozed",
) -> JSONResponse | None:
    """Run the shared consume-then-apply keep sequence.

    The public snooze and the admin "forever" routes both consume the keep token, run their
    guarded UPDATE and write one audit row in a single transaction, so all three land or
    none do. Only the UPDATE and audit detail differ, so they are injected via apply and
    audit_detail.

    Returns a 409 Response when the token was already consumed (concurrent replay) or the
    guarded UPDATE matched no row (the action advanced since the pre-flight check); None on
    success.
    """
    consumed = False
    applied_rowcount = 0
    with conn:
        consumed = mark_token_consumed(conn, token, now)
        if consumed:
            applied_rowcount = apply()
            if applied_rowcount > 0:
                log_audit(conn, verified.media_item_id, audit_action, audit_detail)

    if not consumed:
        # Token hash was already in keep_tokens_used — concurrent replay.
        return respond_err("already_processed", status=409)
    if applied_rowcount == 0:
        # The scheduled_actions row was already token_used=1, delete_status
        # has advanced, or the deadline passed between our check and here.
        return respond_err("already_processed", status=409)
    return None


_FORM_ERROR_MESSAGES = {
    "already_processed": "This link has already been used, so nothing more was changed.",
    "invalid_or_expired": "This link is no longer valid. It may have expired or the deletion date may have passed.",
    "rate_limited": "Too many requests. Try again later.",
}


_DEFAULT_FORM_ERROR = "Something went wrong, so nothing was changed."


def _wants_html_form(request: Request) -> bool:
    """True for a browser submitting the keep page's plain HTML form."""
    content_type = request.headers.get("content-type", "")
    return content_type.startswith(
        "application/x-www-form-urlencoded"
    ) and "text/html" in request.headers.get("accept", "")


def _submit_failure(
    request: Request,
    error: str,
    status: int,
    token: str = "",
) -> Response:
    """Answer a failed keep post: a readable page for a browser form, JSON otherwise.

    A reused token whose item is now protected shows the protected page, since pressing
    Keep twice (or going back and pressing it again) leaves the item kept.
    """
    if not _wants_html_form(request):
        return respond_err(error, status=status)
    templates = cast(Jinja2Templates, request.app.state.templates)
    if error == "already_processed":
        secret_key = request.app.state.config.secret_key
        # Re-read the row: the one checked before the post may predate the keep.
        row = lookup_verified_action(get_db(), token, secret_key)
        if row is not None:
            context = _keep_context(request, token, row, secret_key)
            if context["state"] == "already_kept":
                return templates.TemplateResponse(request, "keep.html", context, status_code=status)
    return templates.TemplateResponse(
        request,
        "keep.html",
        {
            "state": "error",
            "item": None,
            "error_message": _FORM_ERROR_MESSAGES.get(error, _DEFAULT_FORM_ERROR),
            "is_admin": _is_admin(request),
        },
        status_code=status,
    )


def _keep_state(row: VerifiedKeepAction, now: datetime) -> str:
    """Name the state the keep page shows for a verified action."""
    execute_at = parse_execute_at(row.execute_at, default=now)

    if row.delete_status == "deleting":
        # Files may already be gone, so neither Keep nor "already kept" is true.
        return "deleting"
    if execute_at < now:
        return "expired"
    if row.token_used or row.action != "scheduled_deletion":
        # A keep made elsewhere (Library, whole-show) converts the row and clears
        # token_used, so the row's action says it is kept, not the flag.
        return "already_kept"
    return "active"


def _keep_context(
    request: Request, token: str, row: VerifiedKeepAction, secret_key: str
) -> dict[str, object]:
    """Build the keep page's template context for a verified action."""
    now = now_utc()
    execute_at = parse_execute_at(row.execute_at, default=now)
    state = _keep_state(row, now)

    # Format added_at for display (e.g. "14 May 2025"). The template
    # consumes ``item.<field>`` attribute access, so the dataclass is
    # converted to a plain dict here and the derived display field added.
    item_dict = asdict(row)
    item_dict["added_display"] = format_added_display(item_dict["added_at"])
    watched = (
        get_db()
        .execute("SELECT last_watched_at FROM media_items WHERE id = ?", (row.media_item_id,))
        .fetchone()
    )
    item_dict["last_watched_display"] = format_added_display(watched[0]) if watched else ""

    # Computed here, not in the template: ``execute_at.today()`` is tz-naive and
    # jitters across midnight.
    days_left = _days_left(execute_at, now)

    # The page is opened logged out from an email, so the poster needs a signed URL.
    rating_key = item_dict.get("plex_rating_key")
    poster_url = sign_poster_url(rating_key, secret_key) if rating_key else ""

    return {
        "state": state,
        "item": item_dict,
        "poster_url": poster_url,
        "token": token,
        "is_admin": _is_admin(request),
        "execute_at": execute_at,
        "days_left": days_left,
    }


@router.get("/keep/{token}", response_class=HTMLResponse)
def keep_page(request: Request, token: str) -> HTMLResponse:
    conn = get_db()
    templates = cast(Jinja2Templates, request.app.state.templates)
    config = request.app.state.config

    if not _KEEP_GET_LIMITER.check(get_client_ip(request)):
        return templates.TemplateResponse(
            request,
            "keep.html",
            {
                "state": "error",
                "item": None,
                "error_pill": "Rate limited",
                "error_heading": "Too many requests",
                "error_message": "Wait a minute and open the link again.",
                "is_admin": False,
            },
            status_code=429,
        )
    if len(token) > 4096:
        return templates.TemplateResponse(
            request,
            "keep.html",
            {
                "state": "invalid",
                "item": None,
                "is_admin": False,
            },
        )

    row = lookup_verified_action(conn, token, config.secret_key)

    if row is None:
        return templates.TemplateResponse(
            request,
            "keep.html",
            {
                "state": "invalid",
                "item": None,
                "is_admin": False,
            },
        )

    return templates.TemplateResponse(
        request,
        "keep.html",
        _keep_context(request, token, row, config.secret_key),
    )


@router.post("/keep/{token}", response_class=HTMLResponse)
def keep_submit(request: Request, token: str, duration: str = Form(default="")) -> Response:
    """Apply a snooze via the keep page.

    CSRF-exempt: the route is HMAC-token-authenticated and clicked from email clients, where
    the browser's Origin is whichever webmail host the recipient uses. The exemption is
    opt-in via _CSRF_EXEMPT_ROUTES in mediaman.web, so a sibling POST /keep/... route does
    not inherit it.

    Token invalidation:

    1. HMAC-verify first, so forged tokens never touch the DB.
    2. INSERT the token hash into keep_tokens_used; a suppressed insert means already
      consumed (409).
    3. Update the scheduled action guarded by token_used=0; the rowcount check is a second
      defence against a POST racing step 2.

    duration='forever' is rejected here and must go to POST /api/keep/{token}/forever
    (admin-only, CSRF-protected). Error bodies are deliberately non-informative.
    """
    conn = get_db()
    config = request.app.state.config

    if not _KEEP_POST_LIMITER.check(get_client_ip(request)):
        return _submit_failure(request, "rate_limited", 429)
    if len(token) > 4096:
        return _submit_failure(request, "invalid_or_expired", 400)

    # Reject unknown durations early; also reject "forever" — that lives on
    # the admin-only endpoint for privileged users only.
    if duration not in VALID_KEEP_DURATIONS or duration == "forever":
        return _submit_failure(request, "invalid_or_expired", 400)

    # HMAC-verify the token and confirm it maps to an existing action.
    # Any failure here (bad signature, expired payload, row absent) → 400.
    verified = lookup_verified_action(conn, token, config.secret_key)
    if verified is None:
        return _submit_failure(request, "invalid_or_expired", 400)

    now = now_utc()

    # Pre-flight replay check: if the token hash is already in keep_tokens_used,
    # reject immediately with 409 so a sequential replay (after the scheduled_action
    # row's action has been updated) still gets the correct status code.
    if is_keep_token_consumed(conn, token_hash(token)):
        return _submit_failure(request, "already_processed", 409, token)

    # Pre-flight: confirm the action is pending and the deadline has not
    # passed before entering the write transaction — avoids an empty write
    # on the "obviously expired" path. An item kept another way since the email went
    # out is answered like a repeat keep, not as a dead link.
    if not is_pending_unexpired(verified, now):
        if _keep_state(verified, now) == "already_kept":
            return _submit_failure(request, "already_processed", 409, token)
        return _submit_failure(request, "invalid_or_expired", 400)

    # ``duration`` is non-"forever" by the early reject above, so the
    # mapping is guaranteed to yield an int.
    days = cast(int, VALID_KEEP_DURATIONS[duration])

    failure = _consume_keep_token_and_apply(
        conn,
        token=token,
        verified=verified,
        now=now,
        apply=lambda: apply_keep_snooze(
            conn,
            action_id=verified.id,
            duration=duration,
            days=days,
            now=now,
        ),
        audit_detail=f"Kept for {duration}",
    )
    if failure is not None:
        return _submit_failure(request, "already_processed", 409, token)

    return RedirectResponse(f"/keep/{token}", status_code=302)


@router.post("/api/keep/{token}/forever")
def keep_forever(
    request: Request,
    token: str,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Apply the 'forever' (protected_forever) keep action.

    Admin-only and CSRF-protected (not in the CSRF-exempt list); the public POST
    /keep/{token} returns 400 for duration='forever'. The UPDATE is guarded like the snooze
    path, and the token hash is recorded in keep_tokens_used so replays are rejected even if
    the scheduled_actions row changes concurrently.
    """
    conn = get_db()
    config = request.app.state.config

    if not _KEEP_POST_LIMITER.check(get_client_ip(request)):
        return respond_err("too_many_requests", status=429)
    if len(token) > 4096:
        return respond_err("invalid_or_expired", status=400)

    verified = lookup_verified_action(conn, token, config.secret_key)
    if verified is None:
        return respond_err("invalid_or_expired", status=400)

    now = now_utc()

    # Pre-flight replay check: if the token hash is already in keep_tokens_used,
    # reject immediately with 409 before the is_pending_unexpired check.
    if is_keep_token_consumed(conn, token_hash(token)):
        return respond_err("already_processed", status=409)

    # Pre-flight expiry check before entering the write transaction.
    if not is_pending_unexpired(verified, now):
        if _keep_state(verified, now) == "already_kept":
            return respond_err("already_processed", status=409)
        return respond_err("invalid_or_expired", status=400)

    failure = _consume_keep_token_and_apply(
        conn,
        token=token,
        verified=verified,
        now=now,
        apply=lambda: apply_keep_forever(conn, action_id=verified.id, now=now),
        audit_detail="Kept forever (admin)",
        audit_action="kept",
    )
    if failure is not None:
        return failure

    return respond_ok({"state": "protected_forever"})
