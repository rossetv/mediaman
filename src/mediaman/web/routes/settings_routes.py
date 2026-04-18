"""Settings page and API endpoints.

Handles reading, writing, and testing all application settings. Secret fields
(API keys, tokens, passwords) are stored AES-256-GCM encrypted in the DB and
masked as "****" in GET responses. Non-secret fields are stored as plain JSON.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from mediaman.auth.audit import security_event
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.services.storage import get_disk_usage

# Per-admin rate limits for destructive or high-cost operations.
# Values chosen to be generous for legitimate ops but to cap the
# damage a compromised session can inflict in a short window.
_DELETE_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60, max_per_day=500)
_USER_MGMT_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)
_SETTINGS_WRITE_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60, max_per_day=200)
_NEWSLETTER_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=300, max_per_day=10)
from mediaman.auth.session import (
    authenticate,
    change_password,
    create_user,
    delete_user,
    destroy_all_sessions_for,
    list_sessions_for,
    list_users,
    validate_session,
)
from mediaman.crypto import decrypt_value, encrypt_value
from mediaman.db import get_db


class _CreateUserBody(BaseModel):
    """Body shape for POST /api/users."""

    username: str = ""
    password: str = ""


class _ChangePasswordBody(BaseModel):
    """Body shape for POST /api/users/change-password."""

    old_password: str = ""
    new_password: str = ""


class _ReauthBody(BaseModel):
    """Body shape for endpoints that require password re-confirmation.

    Extends whatever the handler accepts — callers can merge this into
    their request body shape. Kept as a separate class so the pattern
    is visible and consistent.
    """

    confirm_password: str = ""


def _require_reauth(conn, admin: str, password: str) -> bool:
    """Return True if *password* matches *admin*'s current hash.

    Used to gate destructive operations: delete user, delete
    subscriber, send newsletter, change settings that contain
    plaintext secrets. A compromised session cookie WITHOUT the
    password cannot trigger these flows.
    """
    if not password:
        return False
    return authenticate(conn, admin, password)

logger = logging.getLogger("mediaman")

router = APIRouter()

SECRET_FIELDS = {"plex_token", "sonarr_api_key", "radarr_api_key", "nzbget_password", "mailgun_api_key", "tmdb_api_key", "tmdb_read_token", "openai_api_key", "omdb_api_key"}

# All known settings keys — used to initialise missing values gracefully.
# ``*_public_url`` is the optional user-facing URL (what a browser can
# reach) — separate from ``*_url`` which is the internal address
# mediaman itself uses to call the API. When unset, the internal URL
# is used as the public URL as well.
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
    "min_age_days",
    "inactivity_days",
    "grace_days",
    "dry_run",
    "disk_thresholds",
    "suggestions_enabled",
}


#: Internal crypto plumbing rows (HKDF salt, canary) — never shown in the UI.
_INTERNAL_KEYS = {"aes_kdf_salt", "aes_kdf_canary"}


def _load_settings(conn, secret_key: str) -> dict:
    """Return all settings from the DB.

    Secret fields are returned as their decrypted plaintext so callers can use
    them to build service clients. Use :func:`_mask_secrets` before sending
    values to the browser. Internal crypto rows (HKDF salt, canary) are
    filtered out so they never leak to the UI or the JSON API.
    """
    rows = conn.execute("SELECT key, value, encrypted FROM settings").fetchall()
    settings: dict[str, object] = {}
    for row in rows:
        if row["key"] in _INTERNAL_KEYS:
            continue
        raw = row["value"]
        if row["encrypted"]:
            try:
                # Bind the setting key as AAD so ciphertexts cannot be
                # swapped between rows. Falls back to no-AAD decrypt
                # for legacy rows written before this change.
                settings[row["key"]] = decrypt_value(
                    raw, secret_key, conn=conn, aad=row["key"].encode()
                )
            except Exception:
                settings[row["key"]] = ""
        else:
            # Plain values are stored as JSON so booleans and arrays round-trip
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


def _build_plex_client(conn, secret_key: str):
    """Build a PlexClient from stored settings, or return None if not configured.

    Reads ``plex_url`` and ``plex_token`` directly from the settings table,
    decrypting the token if encrypted. Returns ``None`` when either value is
    absent so callers can return a graceful error response without raising.
    """
    from mediaman.services.plex import PlexClient

    url_row = conn.execute(
        "SELECT value FROM settings WHERE key='plex_url'"
    ).fetchone()
    token_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='plex_token'"
    ).fetchone()
    if not url_row or not token_row:
        return None

    url = (url_row["value"] or "").strip()
    token = token_row["value"] or ""
    if token_row["encrypted"]:
        token = decrypt_value(token, secret_key, conn=conn, aad=b"plex_token")
    token = token.strip()

    if not url or not token:
        return None
    return PlexClient(url, token)


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    """Render the settings page. Redirects to /login if session is invalid."""
    token = request.cookies.get("session_token")
    if not token:
        return RedirectResponse("/login", status_code=302)

    conn = get_db()
    username = validate_session(conn, token)
    if username is None:
        return RedirectResponse("/login", status_code=302)

    config = request.app.state.config
    settings = _mask_secrets(_load_settings(conn, config.secret_key))

    # Build library list for the Plex library toggles.
    # plex_libraries is stored as a JSON array of selected library IDs.
    plex_libraries_selected: list[str] = settings.get("plex_libraries", []) or []  # type: ignore[assignment]

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "settings.html", {
        "username": username,
        "nav_active": "settings",
        "settings": settings,
        "plex_libraries_selected": plex_libraries_selected,
    })


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@router.get("/api/settings")
def api_get_settings(request: Request, admin: str = Depends(get_current_admin)):
    """Return all settings as JSON with secret fields masked as '****'."""
    conn = get_db()
    config = request.app.state.config
    settings = _mask_secrets(_load_settings(conn, config.secret_key))
    return JSONResponse(settings)


@router.put("/api/settings")
def api_update_settings(
    request: Request,
    body: dict = Body(...),
    admin: str = Depends(get_current_admin),
):
    """Persist settings from the request body.

    Secret fields are encrypted before storage. If a secret field value is
    "****" or empty, the existing stored value is left untouched.
    """
    if not _SETTINGS_WRITE_LIMITER.check(admin):
        logger.warning("settings.write_throttled user=%s", admin)
        return JSONResponse(
            {"error": "Too many settings changes — slow down"},
            status_code=429,
        )
    conn = get_db()
    config = request.app.state.config
    now = datetime.now(timezone.utc).isoformat()

    # Validate URL-shaped settings before storage. These values flow
    # into outbound HTTP base URLs and, for ``base_url``, into the
    # ``href`` attributes of links in outbound newsletters — a
    # ``javascript:`` or ``data:`` scheme would turn every newsletter
    # into a phishing vector.
    from urllib.parse import urlparse as _urlparse

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
                return JSONResponse(
                    {"error": f"{_url_key} too long"}, status_code=400
                )
            try:
                parsed = _urlparse(_candidate)
            except ValueError:
                parsed = None
            if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc:
                return JSONResponse(
                    {"error": f"{_url_key} must be an http(s) URL"},
                    status_code=400,
                )

    for key, value in body.items():
        if key not in _ALL_KEYS:
            continue
        if value is None:
            continue
        if key in SECRET_FIELDS:
            if value == "****" or value == "":
                continue  # Preserve existing encrypted value
            # Bind the setting key name as GCM AAD so ciphertexts
            # cannot be swapped between rows by a DB-write attacker.
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
    security_event(
        conn, event="settings.write", actor=admin,
        ip=get_client_ip(request),
        detail={"keys": sorted(k for k in body.keys() if k in _ALL_KEYS)},
    )
    return {"status": "saved"}


@router.post("/api/settings/test/{service}")
def api_test_service(service: str, request: Request, admin: str = Depends(get_current_admin)):
    """Test connectivity for a named service using current stored settings.

    Returns ``{"ok": true}`` on success or ``{"ok": false, "error": "..."}``
    on failure. Supported services: plex, sonarr, radarr, nzbget, mailgun.
    """
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
            # PlexServer raises on bad credentials/unreachable — treat any
            # successful instantiation as a connection test.
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
            import requests as http_requests
            api_key = str(settings.get("openai_api_key") or "")
            if not api_key:
                return JSONResponse({"ok": False, "error": "OpenAI API key is required"})
            resp = http_requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return JSONResponse({"ok": True})
            return JSONResponse({"ok": False, "error": f"HTTP {resp.status_code}"})

        elif service == "tmdb":
            import requests as http_requests
            read_token = str(settings.get("tmdb_read_token") or "")
            if not read_token:
                return JSONResponse({"ok": False, "error": "TMDB Read Token is required"})
            resp = http_requests.get(
                "https://api.themoviedb.org/3/configuration",
                headers={"Authorization": f"Bearer {read_token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return JSONResponse({"ok": True})
            return JSONResponse({"ok": False, "error": f"HTTP {resp.status_code}"})

        elif service == "omdb":
            import requests as http_requests
            api_key = str(settings.get("omdb_api_key") or "")
            if not api_key:
                return JSONResponse({"ok": False, "error": "OMDB API key is required"})
            resp = http_requests.get(
                "https://www.omdbapi.com/",
                params={"apikey": api_key, "i": "tt0111161"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("Response") == "True":
                    return JSONResponse({"ok": True})
                return JSONResponse({"ok": False, "error": data.get("Error", "Invalid API key")})
            return JSONResponse({"ok": False, "error": f"HTTP {resp.status_code}"})

        else:
            return JSONResponse({"ok": False, "error": f"Unknown service: {service}"}, status_code=400)

    except Exception as exc:  # noqa: BLE001
        logger.warning("Service test failed for %s: %s", service, exc)
        return JSONResponse({"ok": False, "error": "Service connection test failed"})


@router.get("/api/plex/libraries")
def api_plex_libraries(request: Request, admin: str = Depends(get_current_admin)):
    """Return all Plex library sections available on the configured server.

    Returns ``{"libraries": [...]}`` on success or
    ``{"libraries": [], "error": "..."}`` when Plex is not configured or
    unreachable. The list items are ``{"id", "type", "title"}`` dicts.
    """
    conn = get_db()
    config = request.app.state.config
    try:
        client = _build_plex_client(conn, config.secret_key)
        if client is None:
            return JSONResponse({"libraries": [], "error": "Plex URL and token are not configured"})
        libraries = client.get_libraries()
        return JSONResponse({"libraries": libraries})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch Plex libraries: %s", exc)
        return JSONResponse({"libraries": [], "error": "Failed to fetch Plex libraries"})


def _disk_usage_allowed_paths() -> set[str]:
    """Return the set of filesystem paths the disk-usage endpoint may stat.

    The allow-list is derived from ``MEDIAMAN_DELETE_ROOTS`` (if set),
    ``MEDIAMAN_DATA_DIR``, and the conventional ``/media`` mount. An
    authenticated admin still shouldn't be able to probe arbitrary
    filesystem paths — it's both an information-disclosure primitive
    and a reconnaissance step in any later compromise — so we refuse
    paths outside the known media/data roots even for authed callers.
    """
    import os as _os
    from pathlib import Path as _Path

    allowed: set[str] = set()
    roots = (_os.environ.get("MEDIAMAN_DELETE_ROOTS") or "").strip()
    for token in roots.split(","):
        token = token.strip()
        if token:
            try:
                allowed.add(str(_Path(token).resolve()))
            except (OSError, ValueError):
                continue
    data_dir = _os.environ.get("MEDIAMAN_DATA_DIR", "/data").strip()
    if data_dir:
        try:
            allowed.add(str(_Path(data_dir).resolve()))
        except (OSError, ValueError):
            pass
    # Conventional mount point used by the docker-compose example.
    allowed.add("/media")
    allowed.add("/data")
    return allowed


@router.get("/api/settings/disk-usage")
def api_disk_usage(request: Request, path: str = "", admin: str = Depends(get_current_admin)):
    """Return disk usage stats for a whitelisted filesystem path.

    The *path* parameter is resolved and must equal — or be a
    descendant of — one of the allowed roots (``MEDIAMAN_DELETE_ROOTS``,
    ``MEDIAMAN_DATA_DIR``, or the conventional ``/media`` / ``/data``
    mounts). Anything else is refused with 403 to prevent the endpoint
    being used as an arbitrary-path existence/size oracle.
    """
    if not path:
        return JSONResponse({"error": "path parameter is required"}, status_code=400)
    if len(path) > 4096:
        return JSONResponse({"error": "path too long"}, status_code=400)

    from pathlib import Path as _Path

    try:
        resolved = _Path(path).resolve()
    except (OSError, ValueError):
        return JSONResponse({"error": "invalid path"}, status_code=400)

    allowed_roots = _disk_usage_allowed_paths()
    ok = False
    for root_str in allowed_roots:
        try:
            root_path = _Path(root_str)
        except (OSError, ValueError):
            continue
        if resolved == root_path or root_path in resolved.parents:
            ok = True
            break
    if not ok:
        return JSONResponse(
            {"error": "path not permitted"},
            status_code=403,
        )

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
        return JSONResponse({"error": "path not found"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read disk usage for %s: %s", resolved, exc)
        return JSONResponse({"error": "Failed to read disk usage"})


# ---------------------------------------------------------------------------
# User management API
# ---------------------------------------------------------------------------


@router.get("/api/users")
def api_list_users(admin: str = Depends(get_current_admin)):
    """List all admin users."""
    conn = get_db()
    return {"users": list_users(conn), "current": admin}


@router.post("/api/users")
def api_create_user(
    body: _CreateUserBody,
    admin: str = Depends(get_current_admin),
):
    """Create a new admin user."""
    username = body.username.strip()
    password = body.password

    if not username or len(username) < 3 or len(username) > 64:
        return JSONResponse(
            {"ok": False, "error": "Username must be between 3 and 64 characters"},
            status_code=400,
        )

    from mediaman.auth.password_policy import password_issues
    issues = password_issues(password, username=username)
    if issues:
        return JSONResponse(
            {
                "ok": False,
                "error": "Password does not meet the strength policy",
                "issues": issues,
            },
            status_code=400,
        )

    conn = get_db()
    try:
        create_user(conn, username, password)
        return {"ok": True, "username": username}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)


@router.delete("/api/users/{user_id}")
def api_delete_user(
    user_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
    confirm_password: str = "",
    x_confirm_password: str | None = Header(default=None),
):
    """Delete an admin user. Cannot delete yourself.

    Requires the caller's password, accepted from either:

    - ``X-Confirm-Password`` request header (preferred — keeps the
      password out of URLs and server logs).
    - ``?confirm_password=…`` query string (legacy / curl-friendly).

    A compromised session cookie alone cannot delete other admins.
    """
    conn = get_db()
    if not _USER_MGMT_LIMITER.check(admin):
        return JSONResponse(
            {"ok": False, "error": "Too many user-management operations"},
            status_code=429,
        )
    # Prefer the header if the client sent one; fall back to query.
    pw = x_confirm_password if x_confirm_password is not None else confirm_password
    if not _require_reauth(conn, admin, pw):
        logger.warning("user.delete_rejected user=%s reason=reauth_required", admin)
        return JSONResponse(
            {"ok": False, "error": "Password confirmation required"},
            status_code=403,
        )
    if delete_user(conn, user_id, admin):
        logger.info("user.deleted actor=%s target_id=%d", admin, user_id)
        security_event(
            conn, event="user.deleted", actor=admin,
            ip=get_client_ip(request),
            detail={"target_id": user_id},
        )
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "Cannot delete yourself or user not found"}, status_code=400)


@router.post("/api/users/change-password")
def api_change_password(
    request: Request,
    body: _ChangePasswordBody,
    admin: str = Depends(get_current_admin),
):
    """Change the current user's password."""
    old_password = body.old_password
    new_password = body.new_password

    if new_password == old_password:
        return JSONResponse(
            {"ok": False, "error": "New password must differ from the old password"},
            status_code=400,
        )

    from mediaman.auth.password_policy import password_issues
    issues = password_issues(new_password, username=admin)
    if issues:
        return JSONResponse(
            {
                "ok": False,
                "error": "Password does not meet the strength policy",
                "issues": issues,
            },
            status_code=400,
        )

    conn = get_db()
    if change_password(conn, admin, old_password, new_password):
        # Create a new session since the old ones were invalidated
        from mediaman.auth.rate_limit import get_client_ip
        from mediaman.auth.session import create_session
        new_token = create_session(
            conn, admin,
            user_agent=request.headers.get("user-agent", ""),
            client_ip=get_client_ip(request),
        )
        from mediaman.web.routes.auth_routes import _is_request_secure
        response = JSONResponse({"ok": True, "message": "Password changed. You will be re-authenticated."})
        response.set_cookie(
            "session_token", new_token,
            httponly=True, samesite="strict", max_age=86400,
            secure=_is_request_secure(request),
        )
        return response
    logger.warning("password.change_rejected user=%s reason=wrong_old_password", admin)
    return JSONResponse({"ok": False, "error": "Current password is incorrect"}, status_code=403)


@router.get("/api/users/sessions")
def api_list_sessions(admin: str = Depends(get_current_admin)):
    """List active sessions for the current admin.

    Returns metadata only (timestamps, issued IP, fingerprint) — never
    raw tokens. Use `/api/users/sessions/revoke-others` to log out
    other devices.
    """
    conn = get_db()
    return {"sessions": list_sessions_for(conn, admin)}


@router.post("/api/users/sessions/revoke-others")
def api_revoke_other_sessions(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Revoke every session for the current admin EXCEPT the current one.

    Useful after "I think my cookie leaked" — the admin keeps working
    on their current tab while every other device is logged out.
    """
    conn = get_db()
    current_token = request.cookies.get("session_token") or ""
    # Delete all, then re-issue a session bound to the current request
    # (preserves continuity for the admin).
    destroyed = destroy_all_sessions_for(conn, admin)
    from mediaman.auth.rate_limit import get_client_ip
    from mediaman.auth.session import create_session
    new_token = create_session(
        conn, admin,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=get_client_ip(request),
    )
    from mediaman.web.routes.auth_routes import _is_request_secure
    response = JSONResponse({
        "ok": True,
        "revoked": destroyed,
        "message": "Other sessions revoked. You are now logged in with a fresh token.",
    })
    response.set_cookie(
        "session_token", new_token,
        httponly=True, samesite="strict", max_age=86400,
        secure=_is_request_secure(request),
    )
    logger.info("session.revoke_all user=%s revoked=%d", admin, destroyed)
    security_event(
        conn, event="session.revoke_all", actor=admin,
        ip=get_client_ip(request),
        detail={"revoked": destroyed},
    )
    return response
