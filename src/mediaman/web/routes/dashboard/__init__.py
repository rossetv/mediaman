"""The storage panel's JSON endpoints, and `/`, which sends an admin to Watch.

View-model shaping lives in _data and _poster_fanout. Every query lives in
mediaman.web.repository.dashboard, so no SQL belongs here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.core.delete_root_state import read_delete_root_waiting
from mediaman.core.format import format_bytes
from mediaman.db import get_db
from mediaman.scanner.repository import read_delete_allowed_roots_setting, read_setting
from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.web.auth.middleware import (
    LANDING_PATH,
    get_current_admin,
    resolve_admin_page_session,
)
from mediaman.web.repository.dashboard import fetch_reclaimed_chart, sum_reclaimed_bytes
from mediaman.web.routes.dashboard._data import (
    _fetch_recently_deleted,
    _fetch_scheduled,
    _fetch_storage_stats,
    _scheduled_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _show_storage(storage: dict[str, object], plex_configured: bool) -> bool:
    """Without Plex the storage card is hidden only when it has nothing to show."""
    return plex_configured or bool(
        storage["total_bytes"]
        or storage["movies_bytes"]
        or storage["tv_bytes"]
        or storage["anime_bytes"]
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    resolved = resolve_admin_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    return RedirectResponse(LANDING_PATH, status_code=302)


@router.get("/api/dashboard/stats")
def api_dashboard_stats(username: str = Depends(get_current_admin)) -> JSONResponse:
    conn = get_db()
    storage = _fetch_storage_stats(conn)

    plex_configured = plex_is_configured(conn)
    reclaimed_bytes = sum_reclaimed_bytes(conn)

    return JSONResponse(
        {
            "storage": storage,
            "reclaimed_total_bytes": reclaimed_bytes,
            "reclaimed_total": format_bytes(reclaimed_bytes),
            "scheduled": _scheduled_summary(_fetch_scheduled(conn)),
            # From the settings row only: a mount is never probed for it.
            "unavailable_roots": [
                {"path": path, "waiting": waiting}
                for path, waiting in sorted(read_delete_root_waiting(conn).items())
            ],
            "deletions_off": not read_delete_allowed_roots_setting(conn, quiet=True),
            # As the scanner reads it: scans then delete nothing.
            "dry_run": read_setting(conn, "dry_run") == "true",
            "plex_configured": plex_configured,
            "show_storage": _show_storage(storage, plex_configured),
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
