"""Settings routes package."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse as _urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.audit import security_event
from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.auth.rate_limit import get_client_ip
from mediaman.crypto import decrypt_value, encrypt_value
from mediaman.db import get_db
from mediaman.models import _API_KEY_RE, SettingsUpdate
from mediaman.services.arr_build import build_plex_from_db
from mediaman.services.http_client import SafeHTTPClient, SafeHTTPError
from mediaman.services.rate_limits import SETTINGS_WRITE_LIMITER as _SETTINGS_WRITE_LIMITER
from mediaman.services.storage import get_disk_usage
from mediaman.services.time import now_iso
from mediaman.services.url_safety import is_safe_outbound_url

logger = logging.getLogger("mediaman")

router = APIRouter()

SECRET_FIELDS = {"plex_token", "sonarr_api_key", "radarr_api_key", "nzbget_password", "mailgun_api_key", "tmdb_api_key", "tmdb_read_token", "openai_api_key", "omdb_api_key"}

_ALL_KEYS = SECRET_FIELDS | {
    "plex_url",
    "plex_public_url",
    "plex_libraries",
    "sonarr_url",
    "sonarr_public_url",
    "radarr_url",
    "radarr_public_url",
    "nzbget_url",
    "nzbget_public_url",
    "nzbget_username",
    "mailgun_domain",
    "mailgun_from_address",
    "base_url",
    "scan_day",
    "scan_time",
    "scan_timezone",
    "library_sync_interval",
    "min_age_days",
    "inactivity_days",
    "grace_days",
    "dry_run",
    "disk_thresholds",
    "suggestions_enabled",
    "openai_web_search_enabled",
}

#: Internal crypto plumbing rows (HKDF salt, canary) — never shown in the UI.
_INTERNAL_KEYS = {"aes_kdf_salt", "aes_kdf_canary"}

def _load_settings(conn, secret_key: str) -> dict:
    """Return all settings from the DB with secrets decrypted."""
    rows = conn.execute("SELECT key, value, encrypted FROM settings").fetchall()
    settings: dict[str, object] = {}
    for row in rows:
        if row["key"] in _INTERNAL_KEYS:
            continue
        raw = row["value"]
        if row["encrypted"]:
            try:
                settings[row["key"]] = decrypt_value(
                    raw, secret_key, conn=conn, aad=row["key"].encode()
                )
            except Exception:
                settings[row["key"]] = ""
        else:
            try:
                settings[row["key"]] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                settings[row["key"]] = raw
    return settings


def _mask_secrets(settings: dict) -> dict:
    """Return a copy of *settings* with secret fields replaced by '****'."""
    out = dict(settings)
    for key in SECRET_FIELDS:
        if key in out and out[key]:
            out[key] = "****"
    return out

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

@router.post("/api/settings/test/{service}")
def api_test_service(service: str, request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Test connectivity for a named service using current stored settings."""
    conn = get_db()
    config = request.app.state.config
    settings = _load_settings(conn, config.secret_key)

    try:
        if service == "plex":
            from mediaman.services.plex import PlexClient
            url = str(settings.get("plex_url") or "")
            token = str(settings.get("plex_token") or "")
            if not url or not token:
                return JSONResponse({"ok": False, "error": "Plex URL and token are required"})
            client = PlexClient(url, token)
            client.get_libraries()
            return JSONResponse({"ok": True})

        elif service == "sonarr":
            from mediaman.services.sonarr import SonarrClient
            url = str(settings.get("sonarr_url") or "")
            api_key = str(settings.get("sonarr_api_key") or "")
            if not url or not api_key:
                return JSONResponse({"ok": False, "error": "Sonarr URL and API key are required"})
            ok = SonarrClient(url, api_key).test_connection()
            return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})

        elif service == "radarr":
            from mediaman.services.radarr import RadarrClient
            url = str(settings.get("radarr_url") or "")
            api_key = str(settings.get("radarr_api_key") or "")
            if not url or not api_key:
                return JSONResponse({"ok": False, "error": "Radarr URL and API key are required"})
            ok = RadarrClient(url, api_key).test_connection()
            return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})

        elif service == "nzbget":
            from mediaman.services.nzbget import NzbgetClient
            url = str(settings.get("nzbget_url") or "")
            username = str(settings.get("nzbget_username") or "")
            password = str(settings.get("nzbget_password") or "")
            if not url:
                return JSONResponse({"ok": False, "error": "NZBGet URL is required"})
            ok = NzbgetClient(url, username, password).test_connection()
            return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})

        elif service == "mailgun":
            from mediaman.services.mailgun import MailgunClient
            domain = str(settings.get("mailgun_domain") or "")
            api_key = str(settings.get("mailgun_api_key") or "")
            from_address = str(settings.get("mailgun_from_address") or "")
            if not domain or not api_key:
                return JSONResponse({"ok": False, "error": "Mailgun domain and API key are required"})
            ok = MailgunClient(domain, api_key, from_address).test_connection()
            return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})

        elif service == "openai":
            api_key = str(settings.get("openai_api_key") or "")
            if not api_key:
                return JSONResponse({"ok": False, "error": "OpenAI API key is required"})
            if not _API_KEY_RE.match(api_key):
                return JSONResponse({"ok": False, "error": "auth_failed: API key contains invalid characters"})
            try:
                SafeHTTPClient().get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=(5.0, 15.0),
                )
                return JSONResponse({"ok": True})
            except SafeHTTPError as exc:
                if exc.status_code == 0 and "refused by SSRF" in exc.body_snippet:
                    return JSONResponse({"ok": False, "error": "ssrf_refused"})
                if exc.status_code == 0 and "transport error" in exc.body_snippet:
                    err_lower = exc.body_snippet.lower()
                    if "timeout" in err_lower:
                        return JSONResponse({"ok": False, "error": "timeout"})
                    return JSONResponse({"ok": False, "error": "connection_refused"})
                if exc.status_code in (401, 403):
                    return JSONResponse({"ok": False, "error": "auth_failed"})
                return JSONResponse({"ok": False, "error": f"other: HTTP {exc.status_code}"})

        elif service == "tmdb":
            read_token = str(settings.get("tmdb_read_token") or "")
            if not read_token:
                return JSONResponse({"ok": False, "error": "TMDB Read Token is required"})
            if not _API_KEY_RE.match(read_token):
                return JSONResponse({"ok": False, "error": "auth_failed: token contains invalid characters"})
            try:
                SafeHTTPClient().get(
                    "https://api.themoviedb.org/3/configuration",
                    headers={"Authorization": f"Bearer {read_token}"},
                    timeout=(5.0, 15.0),
                )
                return JSONResponse({"ok": True})
            except SafeHTTPError as exc:
                if exc.status_code == 0 and "refused by SSRF" in exc.body_snippet:
                    return JSONResponse({"ok": False, "error": "ssrf_refused"})
                if exc.status_code == 0 and "transport error" in exc.body_snippet:
                    err_lower = exc.body_snippet.lower()
                    if "timeout" in err_lower:
                        return JSONResponse({"ok": False, "error": "timeout"})
                    return JSONResponse({"ok": False, "error": "connection_refused"})
                if exc.status_code in (401, 403):
                    return JSONResponse({"ok": False, "error": "auth_failed"})
                return JSONResponse({"ok": False, "error": f"other: HTTP {exc.status_code}"})

        elif service == "omdb":
            api_key = str(settings.get("omdb_api_key") or "")
            if not api_key:
                return JSONResponse({"ok": False, "error": "OMDB API key is required"})
            if not _API_KEY_RE.match(api_key):
                return JSONResponse({"ok": False, "error": "auth_failed: API key contains invalid characters"})
            try:
                resp = SafeHTTPClient().get(
                    "https://www.omdbapi.com/",
                    params={"apikey": api_key, "i": "tt0111161"},
                    timeout=(5.0, 15.0),
                )
                data = resp.json()
                if data.get("Response") == "True":
                    return JSONResponse({"ok": True})
                return JSONResponse({"ok": False, "error": data.get("Error", "auth_failed")})
            except SafeHTTPError as exc:
                if exc.status_code == 0 and "refused by SSRF" in exc.body_snippet:
                    return JSONResponse({"ok": False, "error": "ssrf_refused"})
                if exc.status_code == 0 and "transport error" in exc.body_snippet:
                    err_lower = exc.body_snippet.lower()
                    if "timeout" in err_lower:
                        return JSONResponse({"ok": False, "error": "timeout"})
                    return JSONResponse({"ok": False, "error": "connection_refused"})
                if exc.status_code in (401, 403):
                    return JSONResponse({"ok": False, "error": "auth_failed"})
                return JSONResponse({"ok": False, "error": f"other: HTTP {exc.status_code}"})

        else:
            return JSONResponse({"ok": False, "error": f"Unknown service: {service}"}, status_code=400)

    except Exception as exc:  # noqa: BLE001
        logger.warning("Service test failed for %s: %s", service, exc)
        return JSONResponse({"ok": False, "error": "Service connection test failed"})


@router.get("/api/plex/libraries")
def api_plex_libraries(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all Plex library sections available on the configured server."""
    conn = get_db()
    config = request.app.state.config
    try:
        client = build_plex_from_db(conn, config.secret_key)
        if client is None:
            return JSONResponse({"libraries": [], "error": "Plex URL and token are not configured"})
        libraries = client.get_libraries()
        return JSONResponse({"libraries": libraries})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch Plex libraries: %s", exc)
        return JSONResponse({"libraries": [], "error": "Failed to fetch Plex libraries"})


def _disk_usage_allowed_roots() -> list[Path]:
    """Return the list of filesystem root paths the disk-usage endpoint may stat."""
    roots: list[Path] = []

    def _try_add(raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            p = Path(raw).resolve()
        except (OSError, ValueError):
            return
        roots.append(p)

    for token in (os.environ.get("MEDIAMAN_DELETE_ROOTS") or "").split(","):
        _try_add(token)

    _try_add(os.environ.get("MEDIAMAN_DATA_DIR", ""))
    roots.append(Path("/media"))
    roots.append(Path("/data"))
    return roots


def _resolve_safe_path(raw: str, roots: list[Path]) -> Path | None:
    """Resolve *raw* and verify it is safe to stat."""
    try:
        candidate = Path(raw)
        abs_candidate = Path(os.path.abspath(str(candidate)))
    except (OSError, ValueError):
        return None

    built = Path(abs_candidate.anchor)
    for part in abs_candidate.parts[1:]:
        built = built / part
        try:
            if built.is_symlink():
                return None
        except (OSError, PermissionError):
            return None

    try:
        resolved = abs_candidate.resolve()
    except (OSError, ValueError):
        return None

    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved

    return None


@router.get("/api/settings/disk-usage")
def api_disk_usage(request: Request, path: str = "", admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return disk usage stats for a whitelisted filesystem path."""
    if not path:
        return JSONResponse({"error": "path parameter is required"}, status_code=400)
    if len(path) > 4096:
        return JSONResponse({"error": "path too long"}, status_code=400)

    roots = _disk_usage_allowed_roots()
    resolved = _resolve_safe_path(path, roots)
    if resolved is None:
        return JSONResponse({"error": "not_found"}, status_code=404)

    try:
        usage = get_disk_usage(str(resolved))
        total = usage["total_bytes"]
        used = usage["used_bytes"]
        pct = round(used / total * 100, 1) if total > 0 else 0.0
        return JSONResponse({
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": usage["free_bytes"],
            "usage_pct": pct,
        })
    except FileNotFoundError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read disk usage for %s: %s", resolved, exc)
        return JSONResponse({"error": "Failed to read disk usage"})
