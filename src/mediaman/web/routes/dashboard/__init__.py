"""Dashboard page and supporting JSON API endpoints."""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.core.format import format_bytes
from mediaman.db import get_db
from mediaman.web.auth.middleware import get_current_admin, resolve_page_session
from mediaman.web.routes.dashboard._data import (
    _fetch_recently_deleted,
    _fetch_scheduled,
    _fetch_storage_stats,
)

logger = logging.getLogger("mediaman")

router = APIRouter()

# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request) -> Response:
    """Render the admin dashboard. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    config = request.app.state.config
    scheduled_items = _fetch_scheduled(conn)
    recently_deleted = _fetch_recently_deleted(conn, config.secret_key)
    storage = _fetch_storage_stats(conn)

    # Aggregate totals for section subtitles
    scheduled_count = len(scheduled_items)
    scheduled_size = format_bytes(
        sum(cast(int, i["file_size_bytes"] or 0) for i in scheduled_items)
    )

    # SUM always returns a row; value is NULL when audit_log is empty.
    reclaimed_total_row = conn.execute(
        "SELECT SUM(space_reclaimed_bytes) AS total FROM audit_log WHERE action='deleted'"
    ).fetchone()
    reclaimed_total = format_bytes(reclaimed_total_row["total"] or 0)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "username": username,
            "nav_active": "dashboard",
            "storage": storage,
            "scheduled_items": scheduled_items,
            "scheduled_count": scheduled_count,
            "scheduled_size": scheduled_size,
            "recently_deleted": recently_deleted,
            "reclaimed_total": reclaimed_total,
        },
    )


# ---------------------------------------------------------------------------
# JSON API endpoints
# ---------------------------------------------------------------------------


@router.get("/api/dashboard/stats")
def api_dashboard_stats(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return storage usage and reclaimed-space totals as JSON."""
    conn = get_db()
    storage = _fetch_storage_stats(conn)

    row = conn.execute(
        "SELECT SUM(space_reclaimed_bytes) AS total FROM audit_log WHERE action='deleted'"
    ).fetchone()
    reclaimed_bytes = row["total"] or 0

    return JSONResponse(
        {
            "storage": storage,
            "reclaimed_total_bytes": reclaimed_bytes,
            "reclaimed_total": format_bytes(reclaimed_bytes),
        }
    )


@router.get("/api/dashboard/scheduled")
def api_dashboard_scheduled(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return scheduled-deletion items as JSON."""
    conn = get_db()
    return JSONResponse({"items": _fetch_scheduled(conn)})


@router.get("/api/dashboard/deleted")
def api_dashboard_deleted(
    request: Request, username: str = Depends(get_current_admin)
) -> JSONResponse:
    """Return recently deleted items from audit_log as JSON."""
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    return JSONResponse({"items": _fetch_recently_deleted(conn, secret_key)})


@router.get("/api/dashboard/reclaimed-chart")
def api_dashboard_reclaimed_chart(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return weekly reclaimed-space aggregates grouped by ISO week.

    Each row: { week: 'YYYY-WNN', reclaimed_bytes: int, reclaimed: str }
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT
            strftime('%Y-W%W', created_at) AS week,
            SUM(space_reclaimed_bytes)     AS reclaimed_bytes
        FROM audit_log
        WHERE action = 'deleted'
          AND space_reclaimed_bytes IS NOT NULL
        GROUP BY week
        ORDER BY week DESC
        LIMIT 12
    """).fetchall()

    data = [
        {
            "week": r["week"],
            "reclaimed_bytes": r["reclaimed_bytes"] or 0,
            "reclaimed": format_bytes(r["reclaimed_bytes"] or 0),
        }
        for r in rows
    ]
    return JSONResponse({"weeks": data})


__all__ = [
    "router",
]
