"""Dashboard page and its JSON endpoints.

View-model shaping lives in _data and _poster_fanout. Every query lives in
mediaman.web.repository.dashboard, so no SQL belongs here.
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.delete_root_state import read_delete_root_state
from mediaman.core.format import format_bytes
from mediaman.db import get_db
from mediaman.scanner.repository import read_delete_allowed_roots_setting
from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.web.auth.middleware import get_current_admin, resolve_admin_page_session
from mediaman.web.repository.dashboard import fetch_reclaimed_chart, sum_reclaimed_bytes
from mediaman.web.routes.dashboard._data import (
    _fetch_recently_deleted,
    _fetch_scheduled,
    _fetch_storage_stats,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request) -> Response:
    resolved = resolve_admin_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    config = request.app.state.config
    scheduled_items = _fetch_scheduled(conn)
    recently_deleted = _fetch_recently_deleted(conn, config.secret_key)
    storage = _fetch_storage_stats(conn)

    plex_configured = plex_is_configured(conn)
    # Without Plex the card is hidden only when it has nothing to show.
    show_storage = plex_configured or bool(
        storage["total_bytes"]
        or storage["movies_bytes"]
        or storage["tv_bytes"]
        or storage["anime_bytes"]
    )

    # Aggregate totals for section subtitles
    scheduled_count = len(scheduled_items)
    scheduled_size = format_bytes(
        sum(cast(int, i["file_size_bytes"] or 0) for i in scheduled_items)
    )

    # SUM always returns a row; value is NULL when audit_log is empty.
    reclaimed_total = format_bytes(sum_reclaimed_bytes(conn))

    # Read from the settings row only; the page never touches a mount.
    unavailable_roots = sorted(read_delete_root_state(conn).items())
    deletions_off = not read_delete_allowed_roots_setting(conn, quiet=True)

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "username": username,
            "nav_active": "dashboard",
            "storage": storage,
            "plex_configured": plex_configured,
            "show_storage": show_storage,
            "scheduled_items": scheduled_items,
            "scheduled_count": scheduled_count,
            "scheduled_size": scheduled_size,
            "recently_deleted": recently_deleted,
            "reclaimed_total": reclaimed_total,
            "unavailable_roots": unavailable_roots,
            "deletions_off": deletions_off,
        },
    )


@router.get("/api/dashboard/stats")
def api_dashboard_stats(username: str = Depends(get_current_admin)) -> JSONResponse:
    conn = get_db()
    storage = _fetch_storage_stats(conn)

    reclaimed_bytes = sum_reclaimed_bytes(conn)

    return JSONResponse(
        {
            "storage": storage,
            "reclaimed_total_bytes": reclaimed_bytes,
            "reclaimed_total": format_bytes(reclaimed_bytes),
        }
    )


@router.get("/api/dashboard/scheduled")
def api_dashboard_scheduled(username: str = Depends(get_current_admin)) -> JSONResponse:
    conn = get_db()
    return JSONResponse({"items": _fetch_scheduled(conn)})


@router.get("/api/dashboard/deleted")
def api_dashboard_deleted(
    request: Request, username: str = Depends(get_current_admin)
) -> JSONResponse:
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    return JSONResponse({"items": _fetch_recently_deleted(conn, secret_key)})


@router.get("/api/dashboard/reclaimed-chart")
def api_dashboard_reclaimed_chart(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return weekly reclaimed space by ISO week: {week: 'YYYY-WNN', reclaimed_bytes: int,
    reclaimed: str}.
    """
    conn = get_db()
    weeks = fetch_reclaimed_chart(conn, limit=12)

    data = [
        {
            "week": w.week,
            "reclaimed_bytes": w.reclaimed_bytes,
            "reclaimed": format_bytes(w.reclaimed_bytes),
        }
        for w in weeks
    ]
    return JSONResponse({"weeks": data})


__all__ = [
    "router",
]
