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
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.audit import log_audit
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import RateLimiter, get_client_ip
from mediaman.crypto import validate_keep_token
from mediaman.db import get_db
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED, VALID_KEEP_DURATIONS
from mediaman.web.routes._helpers import is_admin as _is_admin

router = APIRouter()

# Separate rate limiters for GET and POST so an automated prober
# exhausting the GET budget cannot block a legitimate POST (snooze)
# and vice-versa.  Both are bucketed by /24 (v4) / /64 (v6) prefix.
_KEEP_GET_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)
_KEEP_POST_LIMITER = RateLimiter(max_attempts=10, window_seconds=60)


def _token_hash(token: str) -> str:
    """Return a hex SHA-256 digest of *token* for storage in keep_tokens_used."""
    return hashlib.sha256(token.encode()).hexdigest()


def _lookup_verified_action(
    conn: sqlite3.Connection, token: str, secret_key: str
) -> sqlite3.Row | None:
    """Validate the keep-token HMAC, then look up its scheduled action.

    Returns the matching ``scheduled_actions`` row joined with the
    ``media_items`` row, or ``None`` for any failure (bad signature,
    expired token, token/payload mismatch, row absent). Rejecting on
    signature first stops forged tokens reaching the DB lookup at all.

    Finding 16: lookup uses ``token_hash`` first (migration 28 backfills
    existing rows); falls back to raw ``token`` for rows not yet migrated
    so the transition is seamless.
    """
    payload = validate_keep_token(token, secret_key)
    if payload is None:
        return None

    th = _token_hash(token)
    row = conn.execute(
        "SELECT sa.*, mi.title, mi.media_type, mi.poster_path, mi.file_size_bytes, "
        "mi.plex_rating_key, mi.added_at, mi.show_title, mi.season_number "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.token_hash = ?",
        (th,),
    ).fetchone()

    # Fall back to raw token column for rows not yet migrated.
    if row is None:
        row = conn.execute(
            "SELECT sa.*, mi.title, mi.media_type, mi.poster_path, mi.file_size_bytes, "
            "mi.plex_rating_key, mi.added_at, mi.show_title, mi.season_number "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.token = ?",
            (token,),
        ).fetchone()

    if row is None:
        return None

    # The signed payload must reference the same scheduled action as the
    # DB row. Reject any mismatch — a token that validates but points at a
    # different action is tampered and must not be honoured.
    if str(payload.get("media_item_id")) != str(row["media_item_id"]) or int(
        payload.get("action_id", -1)
    ) != int(row["id"]):
        return None

    return row


def find_active_keep_action_by_id_and_token(
    conn: sqlite3.Connection, action_id: int, token: str
) -> sqlite3.Row | None:
    """Look up an active scheduled_deletion row by action_id + token hash.

    This is the hash-based lookup path (Finding 16).  Returns the row if
    ``action='scheduled_deletion'``, ``delete_status='pending'``,
    ``token_used=0``, and the token hash matches; otherwise ``None``.
    Falls back to the raw token column for un-migrated rows.
    """
    th = _token_hash(token)
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT * FROM scheduled_actions "
        "WHERE id = ? AND token_hash = ? "
        "AND action = 'scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status = 'pending') "
        "AND token_used = 0 "
        "AND execute_at >= ?",
        (action_id, th, now),
    ).fetchone()
    if row is not None:
        return row
    # Fall back to raw token for un-migrated rows.
    return conn.execute(
        "SELECT * FROM scheduled_actions "
        "WHERE id = ? AND token = ? "
        "AND action = 'scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status = 'pending') "
        "AND token_used = 0 "
        "AND execute_at >= ?",
        (action_id, token, now),
    ).fetchone()


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

    row = _lookup_verified_action(conn, token, config.secret_key)

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

    # Determine state
    now = datetime.now(timezone.utc)
    execute_at = datetime.fromisoformat(row["execute_at"]) if row["execute_at"] else now
    if execute_at.tzinfo is None:
        execute_at = execute_at.replace(tzinfo=timezone.utc)

    if execute_at < now:
        state = "expired"
    elif row["token_used"]:
        state = "already_kept"
    else:
        state = "active"

    # Format added_at for display (e.g. "14 May 2025")
    item_dict = dict(row)
    raw_added = item_dict.get("added_at") or ""
    try:
        added_dt = datetime.fromisoformat(str(raw_added))
        item_dict["added_display"] = added_dt.strftime("%-d %B %Y")
    except (ValueError, TypeError):
        item_dict["added_display"] = raw_added[:10] if raw_added else ""

    # Check if admin is logged in
    is_admin = _is_admin(request)

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
            "is_admin": is_admin,
            "execute_at": execute_at,
            "days_left": days_left,
        },
    )


@router.post("/keep/{token}", response_class=HTMLResponse)
def keep_submit(request: Request, token: str, duration: str = Form(default="")) -> Response:
    """Apply a snooze via the keep page.

    Token invalidation strategy (H27):

    1. HMAC-verify the token first — forged tokens never touch the DB.
    2. Attempt to INSERT the token hash into ``keep_tokens_used``.  If
       ``rowcount`` is 0 the token was already consumed → 409.
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
    verified = _lookup_verified_action(conn, token, config.secret_key)
    if verified is None:
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    now = datetime.now(timezone.utc)

    # Check the token-used table first so replays get 409, not 400.
    # INSERT OR IGNORE returns rowcount==0 when the hash is already present.
    th = _token_hash(token)
    used_cursor = conn.execute(
        "INSERT OR IGNORE INTO keep_tokens_used (token_hash, used_at) VALUES (?, ?)",
        (th, now.isoformat()),
    )
    if used_cursor.rowcount == 0:
        conn.commit()
        return HTMLResponse('{"error":"already_processed"}', status_code=409)

    # Finding 13: confirm the action is still pending and the deadline has
    # not passed.  If the deadline has already passed, return the same
    # "invalid_or_expired" error so callers get a clear signal.
    execute_at_raw = verified["execute_at"] or ""
    try:
        execute_at = datetime.fromisoformat(execute_at_raw)
        if execute_at.tzinfo is None:
            execute_at = execute_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        execute_at = now  # treat unparseable as expired

    if execute_at < now:
        conn.rollback()
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    action_val = verified["action"] if "action" in verified.keys() else ""
    delete_status_val = (
        verified["delete_status"] if "delete_status" in verified.keys() else "pending"
    )
    if action_val != "scheduled_deletion" or (
        delete_status_val is not None and delete_status_val != "pending"
    ):
        conn.rollback()
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    row = verified

    days = VALID_KEEP_DURATIONS[duration]
    new_execute = now + timedelta(days=days)  # type: ignore[arg-type]  # days is int for non-forever durations; forever is excluded above
    action_id = row["id"]

    # Finding 13: guard the UPDATE with action, delete_status, token_used,
    # and execute_at so a concurrent mutation or an already-expired row
    # cannot accidentally be applied.
    cursor = conn.execute(
        "UPDATE scheduled_actions SET action=?, token_used=1, "
        "execute_at=?, snoozed_at=?, snooze_duration=? "
        "WHERE id=? AND action='scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status='pending') "
        "AND token_used=0 AND execute_at >= ?",
        (
            ACTION_SNOOZED,
            new_execute.isoformat(),
            now.isoformat(),
            duration,
            action_id,
            now.isoformat(),
        ),
    )

    if cursor.rowcount == 0:
        # The scheduled_actions row was already token_used=1, delete_status
        # has advanced, or the deadline passed between our check and here.
        conn.commit()
        return HTMLResponse('{"error":"already_processed"}', status_code=409)

    # Audit log only fires for the winning request
    log_audit(conn, row["media_item_id"], "snoozed", f"Kept for {duration}")
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
        return JSONResponse({"error": "Too many requests"}, status_code=429)
    if len(token) > 4096:
        return JSONResponse({"error": "invalid_or_expired"}, status_code=400)

    verified = _lookup_verified_action(conn, token, config.secret_key)
    if verified is None:
        return JSONResponse({"error": "invalid_or_expired"}, status_code=400)

    now = datetime.now(timezone.utc)

    # Check replay first so replays get 409, not 400.
    th = _token_hash(token)
    used_cursor = conn.execute(
        "INSERT OR IGNORE INTO keep_tokens_used (token_hash, used_at) VALUES (?, ?)",
        (th, now.isoformat()),
    )
    if used_cursor.rowcount == 0:
        conn.commit()
        return JSONResponse({"error": "already_processed"}, status_code=409)

    execute_at_raw = verified["execute_at"] or ""
    try:
        execute_at = datetime.fromisoformat(execute_at_raw)
        if execute_at.tzinfo is None:
            execute_at = execute_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        execute_at = now

    if execute_at < now:
        conn.rollback()
        return JSONResponse({"error": "invalid_or_expired"}, status_code=400)

    action_val = verified["action"] if "action" in verified.keys() else ""
    delete_status_val = (
        verified["delete_status"] if "delete_status" in verified.keys() else "pending"
    )
    if action_val != "scheduled_deletion" or (
        delete_status_val is not None and delete_status_val != "pending"
    ):
        conn.rollback()
        return JSONResponse({"error": "invalid_or_expired"}, status_code=400)

    row = verified
    action_id = row["id"]

    cursor = conn.execute(
        "UPDATE scheduled_actions SET action=?, token_used=1, "
        "snoozed_at=?, snooze_duration=? "
        "WHERE id=? AND action='scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status='pending') "
        "AND token_used=0 AND execute_at >= ?",
        (
            ACTION_PROTECTED_FOREVER,
            now.isoformat(),
            "forever",
            action_id,
            now.isoformat(),
        ),
    )

    if cursor.rowcount == 0:
        conn.commit()
        return JSONResponse({"error": "already_processed"}, status_code=409)

    log_audit(conn, row["media_item_id"], "snoozed", "Kept forever (admin)")
    conn.commit()

    return JSONResponse({"ok": True, "state": "protected_forever"})
