"""History page — paginated audit log with optional action-type filter."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.core.format import format_bytes
from mediaman.db import get_db
from mediaman.web.auth.middleware import get_current_admin, resolve_page_session
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

# Colour-coded badge classes per action type, rendered into history rows.
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


# Audit-log actions that target a *show* (and so should JOIN against
# kept_shows on al.media_item_id, NOT against media_items).  Plex rating-
# keys are typed by content kind in our DB but the audit row only carries
# the rating-key value — without a content-kind tag the JOIN cannot tell
# a movie/episode rating-key apart from a show rating-key.  Pinning the
# JOIN to the action keeps a hypothetical clash from surfacing a movie's
# title against a "kept show" audit row.
_SHOW_ACTIONS = ("kept_show", "removed_show_keep")

# Control-byte stripper for the audit ``detail`` blob.  Audit rows are
# rendered into the history page UI verbatim, and a future security
# event whose detail carried a CR/LF or terminal escape would wreck a
# ``tail -f`` view of an exported log dump.  Strip everything below
# 0x20 except ``\n`` and ``\t`` (which are visible whitespace in HTML).
_CONTROL_BYTES_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _scrub_detail(detail: str | None) -> str:
    """Return *detail* with terminal-corrupting control bytes removed."""
    if not detail:
        return ""
    return _CONTROL_BYTES_RE.sub("", detail)


def _build_item(r) -> dict:
    """Render an audit_log row into the dict the template/JSON expects.

    Title resolution priority:

    1. Security row — title is the event name (``login.success`` etc.).
    2. JOINed ``mi.title`` (media_items hit) or ``ks.show_title`` (kept_shows hit).
    3. Last-resort regex over ``detail`` for ``'Quoted Title'``.

    The security branch is checked **first** so that if a future
    security-event detail happens to contain a quoted JSON key
    (``"plex_token"``) the regex doesn't pull it into the page as the
    visible title.
    """
    action = r["action"]
    is_security = isinstance(action, str) and action.startswith("sec:")

    if is_security:
        # All sec:* events share a single badge slot; the title slot
        # gets the event name without the "sec:" prefix.
        title = action[4:]
        action_label = action[4:]
        badge = "badge-action-security"
    else:
        title = r["mi_title"] or r["ks_title"]
        if not title and r["detail"]:
            # Try to extract from detail like "Show 'Breaking Bad' kept ..."
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


def _fetch_security_rows(conn, *, page: int, per_page: int) -> list[dict]:
    """Fetch ``sec:*`` audit rows without joining any media tables.

    Security rows have ``media_item_id='_security'`` which never matches
    a media_items.id or kept_shows.show_rating_key, so the JOIN was a
    pure overhead before.  ``idx_audit_log_action`` covers the prefix
    LIKE so the count and the page query are both fast.
    """
    offset = (page - 1) * per_page
    rows = conn.execute(
        """
        SELECT
            al.id,
            al.media_item_id,
            al.action,
            al.detail,
            al.space_reclaimed_bytes,
            al.created_at,
            NULL AS mi_title,
            NULL AS plex_rating_key,
            NULL AS ks_title
        FROM audit_log al
        WHERE al.action LIKE ?
        ORDER BY al.created_at DESC
        LIMIT ? OFFSET ?
        """,
        ("sec:%", per_page, offset),
    ).fetchall()
    return [_build_item(r) for r in rows]


def _fetch_media_rows(conn, *, action: str | None, page: int, per_page: int) -> list[dict]:
    """Fetch media-action rows.  Security rows are NOT excluded by default
    so the unfiltered history view still surfaces them — the JOIN
    conditions skip ``_security`` rows so the JOINs are not wasted, and
    only the right table joins for show-vs-movie audit rows."""
    where_sql, where_params = _media_where_clause(action)

    offset = (page - 1) * per_page
    show_action_placeholders = ",".join("?" * len(_SHOW_ACTIONS))
    params = (
        *_SHOW_ACTIONS,  # for media_items NOT-IN
        *_SHOW_ACTIONS,  # for kept_shows IN
        *where_params,
        per_page,
        offset,
    )
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
        LEFT JOIN media_items mi
          ON mi.id = al.media_item_id
            AND al.action NOT IN ({show_action_placeholders})
            AND al.media_item_id != '_security'
        LEFT JOIN kept_shows ks
          ON ks.show_rating_key = al.media_item_id
            AND al.action IN ({show_action_placeholders})
        {where_sql}
        ORDER BY al.created_at DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [_build_item(r) for r in rows]


def _media_where_clause(action: str | None) -> tuple[str, tuple]:
    """Translate a filter name to a (WHERE SQL, params) pair.

    The ``kept`` and ``unkept`` filters expand to multi-action IN
    clauses so the synthetic UI label matches both the legacy and the
    current DB action names.
    """
    _FILTER_MAP = {
        "kept": ("protected", ACTION_PROTECTED_FOREVER, "kept", "kept_show"),
        "unkept": ("unprotected", "removed_show_keep"),
    }
    if action and action in _FILTER_MAP:
        db_actions = _FILTER_MAP[action]
        placeholders = ",".join("?" * len(db_actions))
        return f"WHERE al.action IN ({placeholders})", db_actions
    if action:
        return "WHERE al.action = ?", (action,)
    return "", ()


def _count_total(conn, action: str | None) -> int:
    """Return the unpaged row count for a given filter.

    Pulled out of the row-fetching helpers so the page route can count
    once, clamp ``page`` to ``total_pages``, and only then run the
    OFFSET query — avoiding a wasteful sweep when a stale URL submits
    ``?page=10000`` against a 30-row filter.
    """
    if action == "security":
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log al WHERE al.action LIKE ?",
            ("sec:%",),
        ).fetchone()
        return row["n"] if row else 0
    where_sql, where_params = _media_where_clause(action)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM audit_log al {where_sql}",
        where_params,
    ).fetchone()
    return row["n"] if row else 0


def _fetch_rows(conn, *, action: str | None, page: int, per_page: int) -> list[dict]:
    """Dispatch to the security-only or media-events row fetcher."""
    if action == "security":
        return _fetch_security_rows(conn, page=page, per_page=per_page)
    return _fetch_media_rows(conn, action=action, page=page, per_page=per_page)


def _fetch_history(conn, action: str | None, page: int, per_page: int) -> tuple[list[dict], int]:
    """Return (items, total_count) for the audit log.

    Filters by action when provided.  Applies LIMIT/OFFSET for pagination.

    The synthetic ``security`` filter selects every ``sec:*`` event in a
    dedicated path (no media-table JOINs).  Every other filter — and the
    no-filter case — runs the media-events query whose JOINs are
    confined to the right kind of audit row (``kept_show`` joins
    kept_shows, every other action joins media_items, security rows
    don't join either).

    The two-query split (one COUNT, one SELECT) is deliberate so the
    page route can clamp an out-of-range ``page`` against ``total_pages``
    before running the SELECT.
    """
    total = _count_total(conn, action)
    items = _fetch_rows(conn, action=action, page=page, per_page=per_page)
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
    # Clamp ``page`` to ``total_pages`` so a hostile / stale URL like
    # ``?page=10000`` doesn't trigger a wasteful OFFSET sweep and a
    # blank page with a wrong "Page 10000 of 3" footer.
    total = _count_total(conn, action_filter)
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
