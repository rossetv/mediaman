"""Settings page route."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import resolve_page_session

from ._helpers import _load_settings, _mask_secrets

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    """Render the settings page. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    config = request.app.state.config
    settings = _mask_secrets(_load_settings(conn, config.secret_key))

    _libs_raw = settings.get("plex_libraries") or []
    plex_libraries_selected: list[str] = list(_libs_raw) if isinstance(_libs_raw, list) else []

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "settings.html", {
        "username": username,
        "nav_active": "settings",
        "settings": settings,
        "plex_libraries_selected": plex_libraries_selected,
    })
