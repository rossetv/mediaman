"""Public keep page — token-authenticated snooze for scheduled deletions."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.audit import log_audit
from mediaman.auth.middleware import get_optional_admin_from_token
from mediaman.auth.rate_limit import RateLimiter, get_client_ip
from mediaman.crypto import validate_keep_token
from mediaman.db import get_db
from mediaman.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED, VALID_KEEP_DURATIONS

router = APIRouter()

# Separate rate limiters for GET and POST so an automated prober
# exhausting the GET budget cannot block a legitimate POST (snooze)
# and vice-versa.  Both are bucketed by /24 (v4) / /64 (v6) prefix.
_KEEP_GET_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)
_KEEP_POST_LIMITER = RateLimiter(max_attempts=10, window_seconds=60)


def _token_hash(token: str) -> str:
    """Return a hex SHA-256 digest of *token* for storage in keep_tokens_used."""
    return hashlib.sha256(token.encode()).hexdigest()


def _lookup_verified_action(conn: sqlite3.Connection, token: str, secret_key: str) -> sqlite3.Row | None:
    """Validate the keep-token HMAC, then look up its scheduled action.

    Returns the matching ``scheduled_actions`` row joined with the
    ``media_items`` row, or ``None`` for any failure (bad signature,
    expired token, token/payload mismatch, row absent). Rejecting on
    signature first stops forged tokens reaching the DB lookup at all.
    """
    payload = validate_keep_token(token, secret_key)
    if payload is None:
        return None

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
    if (
        str(payload.get("media_item_id")) != str(row["media_item_id"])
        or int(payload.get("action_id", -1)) != int(row["id"])
    ):
        return None

    return row


@router.get("/keep/{token}", response_class=HTMLResponse)
def keep_page(request: Request, token: str) -> HTMLResponse:
    """Render the keep page. Three states: active, already-kept, expired."""
    conn = get_db()
    templates = request.app.state.templates
    config = request.app.state.config

    if not _KEEP_GET_LIMITER.check(get_client_ip(request)):
        return HTMLResponse("Too many requests. Try again later.", status_code=429)
    if len(token) > 4096:
        return templates.TemplateResponse(request, "keep.html", {
            "state": "expired",
            "item": None,
            "is_admin": False,
        })

    row = _lookup_verified_action(conn, token, config.secret_key)

    if row is None:
        return templates.TemplateResponse(request, "keep.html", {
            "state": "expired",
            "item": None,
            "is_admin": False,
        })

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
    is_admin = get_optional_admin_from_token(request.cookies.get("session_token"), request=request) is not None

    # Compute "days left" server-side in the scheduled action's timezone
    # (UTC). Doing this in the template via ``execute_at.today()`` fails
    # because ``today()`` is tz-naive and jitters across midnight.
    days_left = (execute_at.date() - now.date()).days

    return templates.TemplateResponse(request, "keep.html", {
        "state": state,
        "item": item_dict,
        "token": token,
        "is_admin": is_admin,
        "execute_at": execute_at,
        "days_left": days_left,
    })


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

    Error bodies are intentionally non-informative: "invalid_or_expired"
    and "already_processed" reveal nothing about internal state.
    """
    conn = get_db()
    config = request.app.state.config

    if not _KEEP_POST_LIMITER.check(get_client_ip(request)):
        return HTMLResponse("Too many requests. Try again later.", status_code=429)
    if len(token) > 4096:
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    # Reject unknown durations early
    if duration not in VALID_KEEP_DURATIONS:
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    # HMAC-verify the token and confirm it maps to an existing action.
    # Any failure here (bad signature, expired payload, row absent) → 400.
    verified = _lookup_verified_action(conn, token, config.secret_key)
    if verified is None:
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    # Mark the token as used.  INSERT OR IGNORE on the PRIMARY KEY lets us
    # detect a replay atomically: rowcount==0 means already inserted.
    now = datetime.now(timezone.utc)
    th = _token_hash(token)
    used_cursor = conn.execute(
        "INSERT OR IGNORE INTO keep_tokens_used (token_hash, used_at) VALUES (?, ?)",
        (th, now.isoformat()),
    )
    if used_cursor.rowcount == 0:
        conn.commit()
        return HTMLResponse('{"error":"already_processed"}', status_code=409)

    row = verified

    # Only admins can use "forever"
    is_admin = get_optional_admin_from_token(request.cookies.get("session_token"), request=request) is not None
    if duration == "forever" and not is_admin:
        # Roll back the token-used insert so the admin can retry later.
        conn.rollback()
        return HTMLResponse('{"error":"invalid_or_expired"}', status_code=400)

    if duration == "forever":
        cursor = conn.execute(
            "UPDATE scheduled_actions SET action=?, token_used=1, "
            "snoozed_at=?, snooze_duration=? WHERE token=? AND token_used=0",
            (ACTION_PROTECTED_FOREVER, now.isoformat(), "forever", token),
        )
    else:
        days = VALID_KEEP_DURATIONS[duration]
        new_execute = now + timedelta(days=days)  # type: ignore[arg-type]
        cursor = conn.execute(
            "UPDATE scheduled_actions SET action=?, token_used=1, "
            "execute_at=?, snoozed_at=?, snooze_duration=? WHERE token=? AND token_used=0",
            (ACTION_SNOOZED, new_execute.isoformat(), now.isoformat(), duration, token),
        )

    if cursor.rowcount == 0:
        # The scheduled_actions row was already token_used=1 (race or re-use).
        # The keep_tokens_used insert already committed above so we treat it as
        # already processed.
        conn.commit()
        return HTMLResponse('{"error":"already_processed"}', status_code=409)

    # Audit log only fires for the winning request
    log_audit(conn, row["media_item_id"], "snoozed", f"Kept for {duration}")
    conn.commit()

    return RedirectResponse(f"/keep/{token}", status_code=302)
