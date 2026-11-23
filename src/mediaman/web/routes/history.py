from __future__ import annotations

import logging
import re
import sqlite3
from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.format import format_bytes, title_from_audit_detail
from mediaman.db import get_db
from mediaman.scanner.repository.audit import (
    AuditRow,
    count_audit_rows,
    fetch_media_audit_rows,
    fetch_security_audit_rows,
)
from mediaman.web.auth.middleware import get_current_admin, resolve_admin_page_session
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SCHEDULED_DELETION, ACTION_SNOOZED

logger = logging.getLogger(__name__)

router = APIRouter()

_PER_PAGE_DEFAULT = 25
_PER_PAGE_MAX = 100

ACTION_TYPES = [
    "scheduled",
    ACTION_SNOOZED,
    "kept",
    "deleted",
    "delete_failed",
    "downloaded",
    "re_downloaded",
    "unkept",
    "kept_show",
    "removed_show_keep",
    "security",
]

# Filters kept working for old links but no longer shown as pills: the "kept"
# and "unkept" pills already cover them.
_PILL_HIDDEN = {ACTION_SNOOZED, "removed_show_keep"}

ACTION_BADGE_CLASS = {
    "scanned": "badge-action-scanned",
    "scheduled": "badge-action-scheduled",
    ACTION_SCHEDULED_DELETION: "badge-action-scheduled",
    ACTION_SNOOZED: "badge-action-protected",
    "kept": "badge-action-protected",
    "protected": "badge-action-protected",
    ACTION_PROTECTED_FOREVER: "badge-action-protected",
    "deleted": "badge-action-deleted",
    "delete_failed": "badge-action-deleted",
    "downloaded": "badge-action-downloaded",
    "re_downloaded": "badge-action-redownloaded",
    "unkept": "badge-action-unprotected",
    "unprotected": "badge-action-unprotected",
    "kept_show": "badge-action-protected",
    "removed_show_keep": "badge-action-unprotected",
    "dry_run_skip": "badge-action-scanned",
    "delete_failed_permanent": "badge-action-deleted",
    "delete_refused": "badge-action-deleted",
    "delete_root_unavailable": "badge-action-deleted",
    "delete_root_available": "badge-action-scanned",
    "delete_incomplete": "badge-action-deleted",
    "delete_folders_failed": "badge-action-deleted",
    "delete_awaiting_scan": "badge-action-scanned",
    "media_missing": "badge-action-scanned",
    "unmonitor_skipped": "badge-action-scanned",
}

ACTION_LABELS = {
    # A snooze is recorded when an admin or viewer keeps an item for a while.
    ACTION_SNOOZED: "kept",
    "protected": "kept",
    ACTION_PROTECTED_FOREVER: "kept",
    "unprotected": "keep removed",
    "unkept": "keep removed",
    ACTION_SCHEDULED_DELETION: "scheduled",
    "kept_show": "kept show",
    "removed_show_keep": "keep removed",
    "dry_run_skip": "dry run: skipped",
    "delete_failed": "delete failed",
    "delete_failed_permanent": "delete failed",
    "delete_refused": "delete refused",
    "media_missing": "media missing",
    "unmonitor_skipped": "unmonitor skipped",
    "delete_root_unavailable": "delete root unavailable",
    "delete_root_available": "delete root available",
    "delete_awaiting_scan": "waiting for scan",
    "delete_incomplete": "delete incomplete",
    "delete_folders_failed": "folder cleanup failed",
    "re_downloaded": "re-downloaded",
}

SECURITY_EVENT_LABELS = {
    "login.success": "Signed in",
    "login.failed": "Sign-in failed",
    "logout": "Signed out",
    "user.created": "User created",
    "user.deleted": "User deleted",
    "user.role_changed": "User role changed",
    "user.email_updated": "User email changed",
    "user.unlocked": "User unlocked",
    "password.changed": "Password changed",
    "password.force_changed": "Password changed",
    "reauth.granted": "Password confirmed",
    "reauth.failed": "Password confirmation failed",
    "session.destroy": "Session ended",
    "session.revoke_all": "All sessions ended",
    "settings.write": "Settings changed",
    "settings.write.throttled": "Settings changes throttled",
    "scan.triggered": "Scan started",
    "scan.cleared": "Scan cleared",
    "library.sync": "Library synced",
    "library.sync.failed": "Library sync failed",
    "newsletter.sent": "Newsletter sent",
    "subscriber.added": "Subscriber added",
    "subscriber.removed": "Subscriber removed",
    "subscriber.opted_out": "Subscriber opted out",
    "aes.canary_failed": "Encryption check failed",
    "auto_abandon.fired": "Download abandoned automatically",
    "password.change_failed": "Password change failed",
    "password.force_change_failed": "Password change failed",
    "password.weak_detected": "Weak password detected",
    "user.delete.rate_limited": "User deletion throttled",
    "user.email_update.rate_limited": "User email change throttled",
}

_SECURITY_WHO_RE = re.compile(r"^actor=(.+?) ip=(\S+)")

_DELETE_ROOT_PREFIX = "_delete_root:"

_CONTROL_BYTES_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _scrub_detail(detail: str | None) -> str:
    """Return detail with terminal-corrupting control bytes removed."""
    if not detail:
        return ""
    return _CONTROL_BYTES_RE.sub("", detail)


def _security_label(event: str) -> str:
    return (
        SECURITY_EVENT_LABELS.get(event) or event.replace(".", " ").replace("_", " ").capitalize()
    )


def _security_detail(detail: str | None) -> str:
    """Return "who from where" for a security row; the raw body is never shown."""
    m = _SECURITY_WHO_RE.match(_scrub_detail(detail))
    if not m:
        return ""
    return " from ".join(p for p in m.groups() if p != "-")


def _build_item(r: AuditRow) -> dict[str, object]:
    action = r.action
    is_security = isinstance(action, str) and action.startswith("sec:")

    if is_security:
        title = _security_label(action[4:])
        action_label = "security"
        badge = "badge-action-security"
    else:
        resolved_title = r.mi_title or r.ks_title
        if not resolved_title and r.detail:
            resolved_title = title_from_audit_detail(r.detail, strict=True)
            if resolved_title == "Unknown":
                m = re.search(r"'([^']+)'", r.detail)
                resolved_title = m.group(1) if m else resolved_title
        title = resolved_title or "Unknown"
        if r.media_item_id.startswith(_DELETE_ROOT_PREFIX):
            title = r.media_item_id[len(_DELETE_ROOT_PREFIX) :]
        action_label = ACTION_LABELS.get(action, action)
        badge = ACTION_BADGE_CLASS.get(action, "badge-action-scanned")

    return {
        "id": r.id,
        "media_item_id": r.media_item_id,
        "action": action,
        "action_label": action_label,
        "badge_class": badge,
        "detail": _security_detail(r.detail) if is_security else _scrub_detail(r.detail),
        "space_impact": format_bytes(r.space_reclaimed_bytes) if r.space_reclaimed_bytes else "",
        "created_at": r.created_at,
        "title": title,
        "plex_rating_key": r.plex_rating_key,
        "is_security": is_security,
    }


def _fetch_rows(
    conn: sqlite3.Connection, *, action: str | None, page: int, per_page: int
) -> list[dict[str, object]]:
    if action == "security":
        rows = fetch_security_audit_rows(conn, page=page, per_page=per_page)
    else:
        rows = fetch_media_audit_rows(conn, action=action, page=page, per_page=per_page)
    return [_build_item(r) for r in rows]


def _fetch_history(
    conn: sqlite3.Connection, action: str | None, page: int, per_page: int
) -> tuple[list[dict[str, object]], int]:
    total = count_audit_rows(conn, action)
    items = _fetch_rows(conn, action=action, page=page, per_page=per_page)
    return items, total


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> Response:
    resolved = resolve_admin_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    params = request.query_params
    action_filter = params.get("action") or None
    if action_filter and action_filter not in ACTION_TYPES:
        action_filter = None

    try:
        page = max(1, int(params.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    per_page = _PER_PAGE_DEFAULT
    total = count_audit_rows(conn, action_filter)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    items = _fetch_rows(conn, action=action_filter, page=page, per_page=per_page)

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "username": username,
            "nav_active": "history",
            "items": items,
            "action_types": [
                (at, ACTION_LABELS.get(at, at.replace("_", " ")).capitalize())
                for at in ACTION_TYPES
                if at not in _PILL_HIDDEN
            ],
            "action_filter": action_filter or "",
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
    )


@router.get("/api/history")
def api_history(
    action: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=_PER_PAGE_DEFAULT, ge=1, le=_PER_PAGE_MAX),
    _admin: str = Depends(get_current_admin),
) -> JSONResponse:
    if action and action not in ACTION_TYPES:
        action = None

    conn = get_db()
    items, total = _fetch_history(conn, action, page, per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return JSONResponse(
        {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )


@router.get("/api/security-events")
def api_security_events(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=_PER_PAGE_DEFAULT, ge=1, le=_PER_PAGE_MAX),
    _admin: str = Depends(get_current_admin),
) -> JSONResponse:
    conn = get_db()
    items, total = _fetch_history(conn, "security", page, per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return JSONResponse(
        {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )
