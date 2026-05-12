"""History page -- paginated audit log with optional action-type filter."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.core.format import format_bytes
from mediaman.db import get_db
from mediaman.scanner.repository.audit import (
    count_audit_rows,
    fetch_media_audit_rows,
    fetch_security_audit_rows,
)
from mediaman.web.auth.middleware import get_current_admin, resolve_page_session
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SCHEDULED_DELETION, ACTION_SNOOZED

logger = logging.getLogger(__name__)

router = APIRouter()

_PER_PAGE_DEFAULT = 25
_PER_PAGE_MAX = 100

ACTION_TYPES = [
    "scanned",
    "scheduled",
    ACTION_SNOOZED,
    "kept",
    "deleted",
    "downloaded",
    "re_downloaded",
    "unkept",
    "kept_show",
    "removed_show_keep",
    "security",
]

ACTION_BADGE_CLASS = {
    "scanned": "badge-action-scanned",
    "scheduled": "badge-action-scheduled",
    ACTION_SCHEDULED_DELETION: "badge-action-scheduled",
    ACTION_SNOOZED: "badge-action-snoozed",
    "kept": "badge-action-protected",
    "protected": "badge-action-protected",
    ACTION_PROTECTED_FOREVER: "badge-action-protected",
    "deleted": "badge-action-deleted",
    "downloaded": "badge-action-downloaded",
    "re_downloaded": "badge-action-redownloaded",
    "unkept": "badge-action-unprotected",
    "unprotected": "badge-action-unprotected",
    "kept_show": "badge-action-protected",
    "removed_show_keep": "badge-action-unprotected",
    "dry_run_skip": "badge-action-scanned",
}

ACTION_LABELS = {
    "protected": "kept",
    ACTION_PROTECTED_FOREVER: "kept",
    "unprotected": "unkept",
    ACTION_SCHEDULED_DELETION: "scheduled",
    "kept_show": "kept show",
    "removed_show_keep": "unkept show",
    "dry_run_skip": "dry run",
}

_CONTROL_BYTES_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _scrub_detail(detail: str | None) -> str:
    """Return detail with terminal-corrupting control bytes removed."""
    if not detail:
        return ""
    return _CONTROL_BYTES_RE.sub("", detail)


def _build_item(r) -> dict[str, object]:
    """Render an audit_log row into the dict the template/JSON expects."""
    action = r["action"]
    is_security = isinstance(action, str) and action.startswith("sec:")

    if is_security:
        title = action[4:]
        action_label = action[4:]
        badge = "badge-action-security"
    else:
        title = r["mi_title"] or r["ks_title"]
        if not title and r["detail"]:
            m = re.search(r"'([^']+)'", r["detail"])
            if m:
                title = m.group(1)
        title = title or "Unknown"
        action_label = ACTION_LABELS.get(action, action)
        badge = ACTION_BADGE_CLASS.get(action, "badge-action-scanned")

    return {
        "id": r["id"],
        "media_item_id": r["media_item_id"],
        "action": action,
        "action_label": action_label,
        "badge_class": badge,
        "detail": _scrub_detail(r["detail"]),
        "space_impact": format_bytes(r["space_reclaimed_bytes"])
        if r["space_reclaimed_bytes"]
        else "",
        "created_at": r["created_at"],
        "title": title,
        "plex_rating_key": r["plex_rating_key"],
        "is_security": is_security,
    }


def _fetch_rows(conn, *, action: str | None, page: int, per_page: int) -> list[dict[str, object]]:
    """Dispatch to the security-only or media-events row fetcher."""
    if action == "security":
        rows = fetch_security_audit_rows(conn, page=page, per_page=per_page)
    else:
        rows = fetch_media_audit_rows(conn, action=action, page=page, per_page=per_page)
    return [_build_item(r) for r in rows]


def _fetch_history(conn, action: str | None, page: int, per_page: int) -> tuple[list[dict[str, object]], int]:
    """Return (items, total_count) for the audit log."""
    total = count_audit_rows(conn, action)
    items = _fetch_rows(conn, action=action, page=page, per_page=per_page)
    return items, total


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> Response:
    """Render the audit-log history page. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
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

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "username": username,
            "nav_active": "history",
            "items": items,
            "action_types": ACTION_TYPES,
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
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return paginated audit log as JSON."""
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
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return paginated sec:* audit rows."""
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
