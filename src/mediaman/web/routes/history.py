"""History page — paginated audit log with optional action-type filter."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.db import get_db
from mediaman.services.infra.format import format_bytes
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SCHEDULED_DELETION, ACTION_SNOOZED

logger = logging.getLogger("mediaman")

router = APIRouter()

# Shared per_page bounds used by both the page route and the JSON API so
# they cannot silently diverge.  Default is 25 (sensible page size);
# maximum is 100 (caps DB work per request).
_PER_PAGE_DEFAULT = 25
_PER_PAGE_MAX = 100

# All action types that appear in audit_log.
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
    # Synthetic filter — matches every audit row whose action begins with
    # "sec:" (login.success, settings.write, user.created, password.changed,
    # reauth.granted, …). The DB never stores the literal "security" value;
    # the filter is translated to a LIKE pattern in :func:`_fetch_history`.
    "security",
]

# Colour-coded badge classes per action type (mapped to CSS vars in style.css).
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

# Display labels — rename legacy action names for the UI
ACTION_LABELS = {
    "protected": "kept",
    ACTION_PROTECTED_FOREVER: "kept",
    "unprotected": "unkept",
    ACTION_SCHEDULED_DELETION: "scheduled",
    "kept_show": "kept show",
    "removed_show_keep": "unkept show",
    "dry_run_skip": "dry run",
}


def _fetch_history(conn, action: str | None, page: int, per_page: int) -> tuple[list[dict], int]:
    """Return (rows, total_count) from audit_log joined with media_items.

    Filters by action when provided. Applies LIMIT/OFFSET for pagination.

    The synthetic ``security`` filter expands to ``action LIKE 'sec:%'`` so
    every security-event row is selected at once — useful when an operator
    is reconstructing what a stolen session did.
    """
    # Map filter names to actual DB action values (handles renamed actions)
    _FILTER_MAP = {
        "kept": ("protected", ACTION_PROTECTED_FOREVER, "kept", "kept_show"),
        "unkept": ("unprotected", "removed_show_keep"),
    }
    if action == "security":
        # Match every sec:* event without listing them all.
        base_where = "WHERE al.action LIKE ?"
        params_count = ("sec:%",)
    elif action and action in _FILTER_MAP:
        db_actions = _FILTER_MAP[action]
        placeholders = ",".join("?" * len(db_actions))
        base_where = f"WHERE al.action IN ({placeholders})"
        params_count = db_actions
    elif action:
        base_where = "WHERE al.action = ?"
        params_count = (action,)
    else:
        base_where = ""
        params_count = ()

    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM audit_log al {base_where}",
        params_count,
    ).fetchone()
    total = total_row["n"] if total_row else 0

    offset = (page - 1) * per_page
    params = (*params_count, per_page, offset)

    rows = conn.execute(
        f"""
        SELECT
            al.id,
            al.media_item_id,
            al.action,
            al.detail,
            al.space_reclaimed_bytes,
            al.created_at,
            mi.title AS mi_title,
            mi.plex_rating_key,
            ks.show_title AS ks_title
        FROM audit_log al
        LEFT JOIN media_items mi ON mi.id = al.media_item_id
        LEFT JOIN kept_shows ks ON ks.show_rating_key = al.media_item_id
        {base_where}
        ORDER BY al.created_at DESC
        LIMIT ? OFFSET ?
    """,
        params,
    ).fetchall()

    items = []
    for r in rows:
        action = r["action"]
        is_security = isinstance(action, str) and action.startswith("sec:")
        # Resolve title: media_items first, then kept_shows, then extract from detail
        title = r["mi_title"] or r["ks_title"]
        if not title and r["detail"]:
            # Try to extract from detail like "Show 'Breaking Bad' kept ..."
            m = re.search(r"'([^']+)'", r["detail"])
            if m:
                title = m.group(1)
        if is_security:
            # For security rows the "title" slot in the UI is the event
            # name (login.success, settings.write, …) — much more useful
            # than "Unknown" or a parsed snippet of the detail string.
            title = action[4:]
        title = title or "Unknown"

        action_label = ACTION_LABELS.get(action, action)
        badge = ACTION_BADGE_CLASS.get(action, "badge-action-scanned")
        if is_security:
            # All sec:* events share a single badge slot; shrink the
            # action_label to the event name without the "sec:" prefix
            # so the existing badge layout doesn't blow up.
            action_label = action[4:]
            badge = "badge-action-security"

        items.append(
            {
                "id": r["id"],
                "media_item_id": r["media_item_id"],
                "action": action,
                "action_label": action_label,
                "badge_class": badge,
                "detail": r["detail"] or "",
                "space_impact": format_bytes(r["space_reclaimed_bytes"])
                if r["space_reclaimed_bytes"]
                else "",
                "created_at": r["created_at"],
                "title": title,
                "plex_rating_key": r["plex_rating_key"],
                "is_security": is_security,
            }
        )
    return items, total


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> Response:
    """Render the audit-log history page. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    # Parse query params with safe defaults.
    params = request.query_params
    action_filter = params.get("action") or None
    if action_filter and action_filter not in ACTION_TYPES:
        action_filter = None

    try:
        page = max(1, int(params.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    per_page = _PER_PAGE_DEFAULT
    items, total = _fetch_history(conn, action_filter, page, per_page)

    total_pages = max(1, (total + per_page - 1) // per_page)

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


# ---------------------------------------------------------------------------
# JSON API endpoint
# ---------------------------------------------------------------------------


@router.get("/api/history")
def api_history(
    action: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=_PER_PAGE_DEFAULT, ge=1, le=_PER_PAGE_MAX),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return paginated audit log as JSON.

    Query params:
      - action: filter by action type (optional). Pass ``security`` to
        retrieve every ``sec:*`` event in one query — useful for
        reconstructing what a compromised session did.
      - page: 1-based page number
      - per_page: results per page (max 100)
    """
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
    """Return paginated ``sec:*`` audit rows.

    Convenience wrapper over ``GET /api/history?action=security`` so the
    UI / scripts can fetch the security trail without knowing the
    synthetic filter name. The body shape matches ``api_history`` so a
    front-end can swap endpoints without rewriting the renderer.
    """
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
