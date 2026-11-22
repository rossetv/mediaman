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
                log_audit(conn, verified.media_item_id, "snoozed", audit_detail)

    if not consumed:
        # Token hash was already in keep_tokens_used — concurrent replay.
        return respond_err("already_processed", status=409)
    if applied_rowcount == 0:
        # The scheduled_actions row was already token_used=1, delete_status
        # has advanced, or the deadline passed between our check and here.
        return respond_err("already_processed", status=409)
    return None


@router.get("/keep/{token}", response_class=HTMLResponse)
def keep_page(request: Request, token: str) -> HTMLResponse:
    conn = get_db()
    templates = cast(Jinja2Templates, request.app.state.templates)
    config = request.app.state.config

    if not _KEEP_GET_LIMITER.check(get_client_ip(request)):
        return HTMLResponse("Too many requests. Try again later.", status_code=429)
    if len(token) > 4096:
        return templates.TemplateResponse(
            request,
            "keep.html",
            {
                "state": "expired",
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
                "state": "expired",
                "item": None,
                "is_admin": False,
            },
        )

    # Determine state.
    now = now_utc()
    execute_at = parse_execute_at(row.execute_at, default=now)

    if execute_at < now:
        state = "expired"
    elif row.token_used:
        state = "already_kept"
    else:
        state = "active"

    # Format added_at for display (e.g. "14 May 2025"). The template
    # consumes ``item.<field>`` attribute access, so the dataclass is
    # converted to a plain dict here and the derived display field added.
    item_dict = asdict(row)
    item_dict["added_display"] = format_added_display(item_dict["added_at"])

    # Computed here, not in the template: ``execute_at.today()`` is tz-naive and
    # jitters across midnight.
    days_left = _days_left(execute_at, now)

    # The page is opened logged out from an email, so the poster needs a signed URL.
    rating_key = item_dict.get("plex_rating_key")
    poster_url = sign_poster_url(rating_key, config.secret_key) if rating_key else ""

    return templates.TemplateResponse(
        request,
        "keep.html",
        {
            "state": state,
            "item": item_dict,
            "poster_url": poster_url,
            "token": token,
            "is_admin": _is_admin(request),
            "execute_at": execute_at,
            "days_left": days_left,
        },
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
        return HTMLResponse("Too many requests. Try again later.", status_code=429)
    if len(token) > 4096:
        return respond_err("invalid_or_expired", status=400)

    # Reject unknown durations early; also reject "forever" — that lives on
    # the admin-only endpoint for privileged users only.
    if duration not in VALID_KEEP_DURATIONS or duration == "forever":
        return respond_err("invalid_or_expired", status=400)

    # HMAC-verify the token and confirm it maps to an existing action.
    # Any failure here (bad signature, expired payload, row absent) → 400.
    verified = lookup_verified_action(conn, token, config.secret_key)
    if verified is None:
        return respond_err("invalid_or_expired", status=400)

    now = now_utc()

    # Pre-flight replay check: if the token hash is already in keep_tokens_used,
    # reject immediately with 409 so a sequential replay (after the scheduled_action
    # row's action has been updated) still gets the correct status code.
    if is_keep_token_consumed(conn, token_hash(token)):
        return respond_err("already_processed", status=409)

    # Pre-flight: confirm the action is pending and the deadline has not
    # passed before entering the write transaction — avoids an empty write
    # on the "obviously expired" path.
    if not is_pending_unexpired(verified, now):
        return respond_err("invalid_or_expired", status=400)

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
        return failure

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
        return respond_err("invalid_or_expired", status=400)

    failure = _consume_keep_token_and_apply(
        conn,
        token=token,
        verified=verified,
        now=now,
        apply=lambda: apply_keep_forever(conn, action_id=verified.id, now=now),
        audit_detail="Kept forever (admin)",
    )
    if failure is not None:
        return failure

    return respond_ok({"state": "protected_forever"})
