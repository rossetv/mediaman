"""History page — paginated audit log with optional action-type filter."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.db import get_db
from mediaman.services.format import format_bytes

logger = logging.getLogger("mediaman")

router = APIRouter()

# All action types that appear in audit_log.
ACTION_TYPES = [
    "scanned",
    "scheduled",
    "snoozed",
    "kept",
    "deleted",
    "downloaded",
    "re_downloaded",
    "unkept",
    "kept_show",
    "removed_show_keep",
]

# Colour-coded badge classes per action type (mapped to CSS vars in style.css).
ACTION_BADGE_CLASS = {
    "scanned":            "badge-action-scanned",
    "scheduled":          "badge-action-scheduled",
    "scheduled_deletion": "badge-action-scheduled",
    "snoozed":            "badge-action-snoozed",
    "kept":               "badge-action-protected",
    "protected":          "badge-action-protected",
    "protected_forever":  "badge-action-protected",
    "deleted":            "badge-action-deleted",
    "downloaded":         "badge-action-downloaded",
    "re_downloaded":      "badge-action-redownloaded",
    "unkept":             "badge-action-unprotected",
    "unprotected":        "badge-action-unprotected",
    "kept_show":          "badge-action-protected",
    "removed_show_keep":  "badge-action-unprotected",
    "dry_run_skip":       "badge-action-scanned",
}

# Display labels — rename legacy action names for the UI
ACTION_LABELS = {
    "protected":          "kept",
    "protected_forever":  "kept",
    "unprotected":        "unkept",
    "scheduled_deletion": "scheduled",
    "kept_show":          "kept show",
    "removed_show_keep":  "unkept show",
    "dry_run_skip":       "dry run",
}


def _fetch_history(conn, action: str | None, page: int, per_page: int) -> tuple[list[dict], int]:
    """Return (rows, total_count) from audit_log joined with media_items.

    Filters by action when provided. Applies LIMIT/OFFSET for pagination.
    """
    # Map filter names to actual DB action values (handles renamed actions)
    _FILTER_MAP = {
        "kept": ("protected", "protected_forever", "kept", "kept_show"),
        "unkept": ("unprotected", "removed_show_keep"),
    }
    if action and action in _FILTER_MAP:
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

    rows = conn.execute(f"""
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
    """, params).fetchall()

    items = []
    for r in rows:
        # Resolve title: media_items first, then kept_shows, then extract from detail
        title = r["mi_title"] or r["ks_title"]
        if not title and r["detail"]:
            # Try to extract from detail like "Show 'Breaking Bad' kept ..."
            import re
            m = re.search(r"'([^']+)'", r["detail"])
            if m:
                title = m.group(1)
        title = title or "Unknown"

        action_label = ACTION_LABELS.get(r["action"], r["action"])

        items.append({
            "id": r["id"],
            "media_item_id": r["media_item_id"],
            "action": r["action"],
            "action_label": action_label,
            "badge_class": ACTION_BADGE_CLASS.get(r["action"], "badge-action-scanned"),
            "detail": r["detail"] or "",
            "space_impact": format_bytes(r["space_reclaimed_bytes"]) if r["space_reclaimed_bytes"] else "",
            "created_at": r["created_at"],
            "title": title,
            "plex_rating_key": r["plex_rating_key"],
        })
    return items, total


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
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

    per_page = 25
    items, total = _fetch_history(conn, action_filter, page, per_page)

    total_pages = max(1, (total + per_page - 1) // per_page)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "history.html", {
        "username": username,
        "nav_active": "history",
        "items": items,
        "action_types": ACTION_TYPES,
        "action_filter": action_filter or "",
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })


# ---------------------------------------------------------------------------
# JSON API endpoint
# ---------------------------------------------------------------------------

@router.get("/api/history")
def api_history(
    action: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    username: str = Depends(get_current_admin),
):
    """Return paginated audit log as JSON.

    Query params:
      - action: filter by action type (optional)
      - page: 1-based page number
      - per_page: results per page (max 100)
    """
    if action and action not in ACTION_TYPES:
        action = None

    conn = get_db()
    items, total = _fetch_history(conn, action, page, per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return JSONResponse({
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })
