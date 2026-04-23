"""Settings CRUD API endpoints."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse as _urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from mediaman.auth.audit import security_event
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import get_client_ip
from mediaman.crypto import encrypt_value
from mediaman.db import get_db
from mediaman.models import SettingsUpdate
from mediaman.services.rate_limits import SETTINGS_WRITE_LIMITER as _SETTINGS_WRITE_LIMITER
from mediaman.services.time import now_iso
from mediaman.services.url_safety import is_safe_outbound_url

from ._helpers import _ALL_KEYS, SECRET_FIELDS, _load_settings, _mask_secrets

logger = logging.getLogger("mediaman")

router = APIRouter()


@router.get("/api/settings")
def api_get_settings(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all settings as JSON with secret fields masked as '****'."""
    conn = get_db()
    config = request.app.state.config
    settings = _mask_secrets(_load_settings(conn, config.secret_key))
    return JSONResponse(settings)


@router.put("/api/settings")
def api_update_settings(
    request: Request,
    body: SettingsUpdate,
    admin: str = Depends(get_current_admin),
) -> Response:
    """Persist settings from the request body."""
    body: dict = body.model_dump(exclude_none=True)  # type: ignore[no-redef]
    if not _SETTINGS_WRITE_LIMITER.check(admin):
        logger.warning("settings.write_throttled user=%s", admin)
        return JSONResponse(
            {"error": "Too many settings changes — slow down"},
            status_code=429,
        )
    conn = get_db()
    config = request.app.state.config
    now = now_iso()

    _URL_FIELDS = {
        "base_url",
        "plex_url", "plex_public_url",
        "sonarr_url", "sonarr_public_url",
        "radarr_url", "radarr_public_url",
        "nzbget_url", "nzbget_public_url",
    }

    for _url_key in _URL_FIELDS:
        if _url_key in body and body[_url_key]:
            _candidate = str(body[_url_key]).strip()
            if len(_candidate) > 2048:
                return JSONResponse({"error": f"{_url_key} too long"}, status_code=400)
            try:
                parsed = _urlparse(_candidate)
            except ValueError:
                parsed = None
            if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc:
                return JSONResponse(
                    {"error": f"{_url_key} must be an http(s) URL"},
                    status_code=400,
                )
            if not is_safe_outbound_url(_candidate):
                logger.warning("settings.ssrf_blocked key=%s value=%s", _url_key, _candidate)
                return JSONResponse(
                    {"error": f"{_url_key} points at a blocked address"},
                    status_code=400,
                )

    for key, value in body.items():
        if key not in _ALL_KEYS:
            continue
        if value is None:
            continue
        if key in SECRET_FIELDS:
            if value == "****" or value == "":
                continue
            encrypted_value = encrypt_value(
                str(value), config.secret_key, conn=conn, aad=key.encode()
            )
            conn.execute(
                "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=1, updated_at=excluded.updated_at",
                (key, encrypted_value, now),
            )
        else:
            str_value = json.dumps(value) if isinstance(value, (list, dict, bool)) else str(value)
            conn.execute(
                "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=0, updated_at=excluded.updated_at",
                (key, str_value, now),
            )

    conn.commit()
    written = sorted(k for k in body.keys() if k in _ALL_KEYS)
    ignored = sorted(k for k in body.keys() if k not in _ALL_KEYS)
    security_event(
        conn, event="settings.write", actor=admin,
        ip=get_client_ip(request),
        detail={"keys": written},
    )
    return {"status": "saved", "written": written, "ignored": ignored}
