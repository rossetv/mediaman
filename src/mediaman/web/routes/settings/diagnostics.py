"""Settings diagnostics endpoints."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.auth.middleware import get_current_admin
from mediaman.db import get_db
from mediaman.models import _API_KEY_RE
from mediaman.services.arr_build import build_plex_from_db
from mediaman.services.http_client import SafeHTTPClient, SafeHTTPError
from mediaman.services.storage import get_disk_usage

from ._helpers import _load_settings

logger = logging.getLogger("mediaman")

router = APIRouter()


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
