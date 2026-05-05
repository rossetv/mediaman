"""Public keep page — token-authenticated snooze for scheduled deletions.

Finding 12: "forever" keep is now a separate authenticated, CSRF-protected
endpoint (POST /api/keep/{token}/forever) that requires a valid admin
session rather than accepting the action on the CSRF-exempt public route.

Finding 13: the guarded UPDATE requires action='scheduled_deletion',
delete_status='pending', token_used=0, and execute_at >= now() so that
expired or already-processed rows cannot be snoozed.

Finding 16: only the SHA-256 token hash is written to ``scheduled_actions``
for new snooze rows.  Lookups use ``token_hash`` (joined with signed action
id) instead of the raw token.

The shared verification, parsing, and guarded-UPDATE helpers live in
:mod:`mediaman.services.scheduled_actions` — this file is now thin glue
between FastAPI and that service.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.audit import log_audit
from mediaman.db import get_db
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.services.scheduled_actions import (
    apply_keep_forever,
    apply_keep_snooze,
    find_active_keep_action_by_id_and_token,
    format_added_display,
    is_pending_unexpired,
    lookup_verified_action,
    mark_token_consumed,
    parse_execute_at,
    token_hash,
)
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.models import VALID_KEEP_DURATIONS
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes._helpers import is_admin as _is_admin

router = APIRouter()

# Separate rate limiters for GET and POST so an automated prober
# exhausting the GET budget cannot block a legitimate POST (snooze)
# and vice-versa.  Both are bucketed by /24 (v4) / /64 (v6) prefix.
_KEEP_GET_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)
_KEEP_POST_LIMITER = RateLimiter(max_attempts=10, window_seconds=60)


# Backwards-compatible aliases — existing tests import these names directly
# from this module.  Keep them as re-exports so the test imports continue
# to work without modification.
_token_hash = token_hash
_lookup_verified_action = lookup_verified_action

__all__ = [
    "_KEEP_GET_LIMITER",
    "_KEEP_POST_LIMITER",
    "_lookup_verified_action",
    "_token_hash",
    "find_active_keep_action_by_id_and_token",
    "router",
]


@router.get("/keep/{token}", response_class=HTMLResponse)
def keep_page(request: Request, token: str) -> HTMLResponse:
    """Render the keep page. Three states: active, already-kept, expired."""
    conn = get_db()
    templates = request.app.state.templates
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
    now = datetime.now(UTC)
    execute_at = parse_execute_at(row["execute_at"], default=now)

    if execute_at < now:
        state = "expired"
    elif row["token_used"]:
        state = "already_kept"
    else:
        state = "active"

    # Format added_at for display (e.g. "14 May 2025")
    item_dict = dict(row)
    item_dict["added_display"] = format_added_display(item_dict.get("added_at"))

    # Compute "days left" server-side in the scheduled action's timezone
    # (UTC). Doing this in the template via ``execute_at.today()`` fails
    # because ``today()`` is tz-naive and jitters across midnight.
    days_left = (execute_at.date() - now.date()).days

    return templates.TemplateResponse(
        request,
        "keep.html",
        {
            "state": state,
            "item": item_dict,
            "token": token,
            "is_admin": _is_admin(request),
            "execute_at": execute_at,
            "days_left": days_left,
        },
    )


@router.post("/keep/{token}", response_class=HTMLResponse)
def keep_submit(request: Request, token: str, duration: str = Form(default="")) -> Response:
    """Apply a snooze via the keep page.

    CSRF-exempt: this route is HMAC-token-authenticated and gets clicked
    through from email clients where the browser's Origin is whichever
    webmail host the recipient happens to use.  The exemption is opt-in
    via the explicit ``_CSRF_EXEMPT_ROUTES`` allowlist in
    :mod:`mediaman.web` — adding a sibling ``POST /keep/...`` will NOT
    silently inherit the exemption.

    Token invalidation strategy (H27):

    1. HMAC-verify the token first — forged tokens never touch the DB.
    2. Attempt to INSERT the token hash into ``keep_tokens_used``.  If
       the insert is suppressed the token was already consumed → 409.
    3. Update the scheduled action guarded by ``token_used=0`` — the
       ``rowcount`` check is a second defence against a concurrent POST
       racing step 2.

    Finding 12: ``duration='forever'`` is rejected here and must be submitted
    to ``POST /api/keep/{token}/forever`` instead (admin-only, CSRF-protected).

    Finding 13: the guarded UPDATE requires action='scheduled_deletion',
    delete_status='pending', and execute_at >= now so an expired or
    non-pending row cannot be acted upon.

    Error bodies are intentionally non-informative: "invalid_or_expired"
    and "already_processed" reveal nothing about internal state.
    """
    conn = get_db()
    config = request.app.state.config

    if not _KEEP_POST_LIMITER.check(get_client_ip(request)):
        return HTMLResponse("Too many requests. Try again later.", status_code=429)
    if len(token) > 4096:
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    # Reject unknown durations early; also reject "forever" — that lives on
    # the admin-only endpoint (finding 12).
    if duration not in VALID_KEEP_DURATIONS or duration == "forever":
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    # HMAC-verify the token and confirm it maps to an existing action.
    # Any failure here (bad signature, expired payload, row absent) → 400.
    verified = lookup_verified_action(conn, token, config.secret_key)
    if verified is None:
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    now = datetime.now(UTC)

    # Check the token-used table first so replays get 409, not 400.
    if not mark_token_consumed(conn, token, now):
        conn.commit()
        return HTMLResponse('{"error":"already_processed"}', status_code=409)

    # Finding 13: confirm the action is still pending and the deadline has
    # not passed.  If the deadline has already passed, return the same
    # "invalid_or_expired" error so callers get a clear signal.
    if not is_pending_unexpired(verified, now):
        conn.rollback()
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    # ``duration`` is non-"forever" by the early reject above, so the
    # mapping is guaranteed to yield an int.
    days = VALID_KEEP_DURATIONS[duration]
    assert days is not None

    rowcount = apply_keep_snooze(
        conn,
        action_id=verified["id"],
        duration=duration,
        days=days,
        now=now,
    )
    if rowcount == 0:
        # The scheduled_actions row was already token_used=1, delete_status
        # has advanced, or the deadline passed between our check and here.
        conn.commit()
        return HTMLResponse('{"error":"already_processed"}', status_code=409)

    # Audit log only fires for the winning request.
    log_audit(conn, verified["media_item_id"], "snoozed", f"Kept for {duration}")
    conn.commit()

    return RedirectResponse(f"/keep/{token}", status_code=302)


@router.post("/api/keep/{token}/forever")
def keep_forever(
    request: Request,
    token: str,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Apply the 'forever' (protected_forever) keep action.

    This endpoint is admin-only and CSRF-protected (it is not in the
    CSRF-exempt prefix list).  The public /keep/{token} POST no longer
    accepts duration='forever' — it returns 400 to any caller that tries.

    Finding 12: separates forever-keep onto a dedicated authenticated route.
    Finding 13: guards the UPDATE with action, delete_status, token_used, and
    execute_at just like the regular snooze path.
    Finding 16: marks the token as consumed via hash in keep_tokens_used.
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

    now = datetime.now(UTC)

    # Check replay first so replays get 409, not 400.
    if not mark_token_consumed(conn, token, now):
        conn.commit()
        return respond_err("already_processed", status=409)

    if not is_pending_unexpired(verified, now):
        conn.rollback()
        return respond_err("invalid_or_expired", status=400)

    rowcount = apply_keep_forever(conn, action_id=verified["id"], now=now)
    if rowcount == 0:
        conn.commit()
        return respond_err("already_processed", status=409)

    log_audit(conn, verified["media_item_id"], "snoozed", "Kept forever (admin)")
    conn.commit()

    return respond_ok({"state": "protected_forever"})
