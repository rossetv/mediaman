"""Settings routes package.

Owns:
- All FastAPI route handlers for settings: GET /settings (page),
  GET /api/settings, PUT /api/settings, POST /api/settings/test/{service},
  GET /api/plex/libraries, GET /api/settings/disk-usage.
- ``_load_settings`` — reads settings rows from the DB, decrypting secrets.
- Re-exports of the public constants and helpers from the sub-modules
  (``secrets``, ``testers``, ``core``) so that the flat import path
  ``from mediaman.web.routes.settings import X`` continues to work for
  anything that was accessible on the old single-file module.

Sub-modules
-----------
``secrets``
    Field sets (SECRET_FIELDS, SENSITIVE_KEYS, etc.), masking helpers,
    and sentinel constants for unchanged/clear secret writes.

``testers``
    Per-service tester functions, the TESTERS registry, per-tester key
    allow-lists, and the in-memory result cache.

``core``
    URL-field validation helpers (_URL_FIELDS, validate_url_fields).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import shutil
import sqlite3

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.audit import security_event, security_event_or_raise
from mediaman.core.time import now_iso
from mediaman.core.url_safety import (
    is_safe_outbound_url as is_safe_outbound_url,  # re-exported for patch targets
)
from mediaman.crypto import decrypt_value, encrypt_value
from mediaman.db import get_db
from mediaman.services.arr.build import build_plex_from_db
from mediaman.services.infra.settings_reader import ConfigDecryptError
from mediaman.services.rate_limit import get_client_ip
from mediaman.services.rate_limit.instances import SETTINGS_TEST_LIMITER as _SETTINGS_TEST_LIMITER
from mediaman.services.rate_limit.instances import SETTINGS_WRITE_LIMITER as _SETTINGS_WRITE_LIMITER
from mediaman.web.auth.middleware import get_current_admin, resolve_page_session
from mediaman.web.auth.reauth import has_recent_reauth
from mediaman.web.models import SettingsUpdate
from mediaman.web.responses import respond_err, respond_ok

# ---------------------------------------------------------------------------
# Sub-module imports — re-exported so the old flat module path still works.
# Tests monkeypatch attributes on this module object; everything they touch
# must live or be re-imported here.
# ---------------------------------------------------------------------------
from mediaman.web.routes.settings.core import (
    _URL_FIELDS as _URL_FIELDS,
)
from mediaman.web.routes.settings.core import (
    _scrub_url_for_log as _scrub_url_for_log,
)
from mediaman.web.routes.settings.core import (
    validate_url_fields as _validate_url_fields,
)
from mediaman.web.routes.settings.secrets import (
    ALL_KEYS as _ALL_KEYS,
)
from mediaman.web.routes.settings.secrets import (
    INTERNAL_KEYS as _INTERNAL_KEYS,
)
from mediaman.web.routes.settings.secrets import (
    SECRET_CLEAR_SENTINEL as _SECRET_CLEAR_SENTINEL,
)
from mediaman.web.routes.settings.secrets import (
    SECRET_FIELDS,
    SENSITIVE_KEYS,
)
from mediaman.web.routes.settings.secrets import (
    SECRET_PLACEHOLDER as _SECRET_PLACEHOLDER,
)
from mediaman.web.routes.settings.secrets import (
    encrypted_keys as _encrypted_keys,
)
from mediaman.web.routes.settings.secrets import (
    mask_encrypted_keys as _mask_encrypted_keys,
)
from mediaman.web.routes.settings.secrets import (
    mask_secrets as _mask_secrets,  # noqa: F401 — kept for backward compat
)
from mediaman.web.routes.settings.secrets import (
    touches_sensitive_keys as _touches_sensitive_keys,
)
from mediaman.web.routes.settings.testers import (
    SERVICE_TESTER_KEYS as _SERVICE_TESTER_KEYS,
)
from mediaman.web.routes.settings.testers import (
    TEST_CACHE as _TEST_CACHE,
)
from mediaman.web.routes.settings.testers import (
    TESTER_TIMEOUT_SECONDS as _TESTER_TIMEOUT_SECONDS,
)
from mediaman.web.routes.settings.testers import (
    TESTERS as _SERVICE_TESTERS,
)
from mediaman.web.routes.settings.testers import (
    cache_get as _cache_get,
)
from mediaman.web.routes.settings.testers import (
    cache_put as _cache_put,
)
from mediaman.web.routes.settings.testers import (
    invalidate_test_cache_for_keys as _invalidate_test_cache_for_keys,
)

# Re-export the cache dict under the name tests import directly.
TEST_CACHE = _TEST_CACHE

logger = logging.getLogger("mediaman")

router = APIRouter()


# ---------------------------------------------------------------------------
# _load_settings lives here (not in secrets.py) because it calls
# ``decrypt_value`` at runtime and several tests monkeypatch that name
# on this module object.  Moving it to a sub-module would break those
# patches.
# ---------------------------------------------------------------------------


def _load_settings(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    keys: set[str] | None = None,
) -> dict[str, object]:
    """Return settings from the DB with secrets decrypted.

    When *keys* is supplied, only those rows are read and decrypted. The
    api_test_service flow uses this so a single-service test does NOT
    decrypt every other secret — minimising the blast radius if any one
    decryption is logged or panics. When *keys* is ``None`` (the default)
    every non-internal row is loaded as before.

    Decryption errors are distinguished from "no value set":

    * If the row exists and is marked encrypted, but decryption fails,
      we raise :class:`ConfigDecryptError` so callers can show a
      meaningful banner instead of silently substituting ``""`` (which
      was previously indistinguishable from a never-saved key — a
      regression hazard once an operator rotates ``MEDIAMAN_SECRET_KEY``).
    * If the row simply does not exist, the key is absent from the
      returned dict (callers already use ``.get(key, "")``).
    """
    if keys is not None:
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            f"SELECT key, value, encrypted FROM settings WHERE key IN ({placeholders})",
            tuple(keys),
        ).fetchall()
    else:
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
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt setting %r — surfacing error to caller",
                    row["key"],
                )
                raise ConfigDecryptError(row["key"], exc) from exc
        else:
            try:
                settings[row["key"]] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                settings[row["key"]] = raw
    return settings


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    """Render the settings page. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    # Skip every encrypted secret — the page only ever shows '****' for
    # them, so decrypting just to throw the plaintext away is wasted
    # work and an unnecessary exposure window.
    enc_keys = _encrypted_keys(conn)
    config = request.app.state.config
    try:
        plain = _load_settings(
            conn,
            config.secret_key,
            keys=set(_ALL_KEYS) - enc_keys,
        )
    except ConfigDecryptError:
        plain = {}
    settings = _mask_encrypted_keys(plain, enc_keys)

    _libs_raw = settings.get("plex_libraries") or []
    plex_libraries_selected: list[str] = list(_libs_raw) if isinstance(_libs_raw, list) else []

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "username": username,
            "nav_active": "settings",
            "settings": settings,
            "plex_libraries_selected": plex_libraries_selected,
        },
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@router.get("/api/settings")
def api_get_settings(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all settings as JSON with secret fields masked as '****'.

    Skips decryption of secret fields — we read the ``encrypted=1`` flag
    instead and emit ``****`` directly.  The plaintext is never needed
    here and decrypting it just to mask it is wasted work + an
    unnecessary exposure window for every secret on every settings GET.
    """
    conn = get_db()
    config = request.app.state.config
    enc_keys = _encrypted_keys(conn)
    try:
        plain = _load_settings(
            conn,
            config.secret_key,
            keys=set(_ALL_KEYS) - enc_keys,
        )
    except ConfigDecryptError:
        return respond_err("settings_decrypt_failed", status=500)
    settings = _mask_encrypted_keys(plain, enc_keys)
    return JSONResponse(settings)


@router.put("/api/settings")
def api_update_settings(
    request: Request,
    body: SettingsUpdate,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Persist settings from the request body.

    Sensitive keys (every secret + every URL field + mail addresses +
    ``base_url``) require a recent-reauth ticket — see
    :data:`SENSITIVE_KEYS` and :func:`_touches_sensitive_keys`. Without
    the ticket the entire PUT is rejected with 403 even when only some
    of the body's keys are sensitive: an attacker mixing one sensitive
    field with several harmless ones must not get a partial write.

    The settings write and the audit row are flushed in the same
    ``BEGIN IMMEDIATE`` transaction via :func:`security_event_or_raise`
    so we never have a "settings changed but no audit trail" outcome
    for high-impact mutations (M27).
    """
    body_dict: dict = body.model_dump(exclude_none=True)
    conn = get_db()
    if not _SETTINGS_WRITE_LIMITER.check(admin):
        logger.warning("settings.write_throttled user=%s", admin)
        security_event(
            conn,
            event="settings.write.throttled",
            actor=admin,
            ip=get_client_ip(request),
            detail={"keys": sorted(k for k in body_dict if k in _ALL_KEYS)},
        )
        return respond_err(
            "too_many_requests", status=429, message="Too many settings changes — slow down"
        )
    config = request.app.state.config
    now = now_iso()

    url_err = _validate_url_fields(body_dict)
    if url_err is not None:
        return url_err

    sensitive_write = _touches_sensitive_keys(body_dict)
    if sensitive_write and not has_recent_reauth(conn, session_token, admin):
        logger.warning("settings.write_rejected user=%s reason=reauth_required", admin)
        return respond_err(
            "reauth_required",
            status=403,
            message="Recent password re-authentication required for sensitive settings",
            reauth_required=True,
        )

    written = sorted(k for k in body_dict if k in _ALL_KEYS)
    ignored = sorted(k for k in body_dict if k not in _ALL_KEYS)
    sensitive_written = sorted(k for k in written if k in SENSITIVE_KEYS)

    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for key, value in body_dict.items():
                if key not in _ALL_KEYS:
                    continue
                if value is None:
                    continue
                if key in SECRET_FIELDS:
                    if value == _SECRET_PLACEHOLDER or value == "":
                        continue
                    if value == _SECRET_CLEAR_SENTINEL:
                        conn.execute("DELETE FROM settings WHERE key=?", (key,))
                        continue
                    encrypted_value = encrypt_value(
                        str(value), config.secret_key, conn=conn, aad=key.encode()
                    )
                    conn.execute(
                        "INSERT INTO settings (key, value, encrypted, updated_at) "
                        "VALUES (?, ?, 1, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                        "encrypted=1, updated_at=excluded.updated_at",
                        (key, encrypted_value, now),
                    )
                else:
                    str_value = (
                        json.dumps(value) if isinstance(value, (list, dict, bool)) else str(value)
                    )
                    conn.execute(
                        "INSERT INTO settings (key, value, encrypted, updated_at) "
                        "VALUES (?, ?, 0, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                        "encrypted=0, updated_at=excluded.updated_at",
                        (key, str_value, now),
                    )

            security_event_or_raise(
                conn,
                event="settings.write",
                actor=admin,
                ip=get_client_ip(request),
                detail={
                    "keys": written,
                    "sensitive_keys": sensitive_written,
                },
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except Exception:
        logger.exception("settings.write failed user=%s", admin)
        return respond_err(
            "internal_error", status=500, message="Internal error during settings write"
        )
    _invalidate_test_cache_for_keys(set(written))
    return respond_ok({"status": "saved", "written": written, "ignored": ignored})


@router.post("/api/settings/test/{service}")
def api_test_service(
    service: str, request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    """Test connectivity for a named service using current stored settings.

    A single unified endpoint replaces eight individual per-service
    endpoints.  The dispatch table :data:`_SERVICE_TESTERS` maps service
    name → tester callable; unknown names return 400.

    Constraints layered on top of the dispatch:

    * Per-admin rate limit (10/min, 60/day) — without it a logged-in
      attacker could chain test calls to flood Plex / Mailgun. The
      limit guards real tester invocations only; cache-served replies
      bypass it so a settings page reload (8 services × cache hit) does
      not eat the whole budget on the second visit.
    * Decryption is restricted to the keys this tester actually needs
      (see :data:`_SERVICE_TESTER_KEYS`).  The previous code decrypted
      every secret in the DB on every test, which is a needless plain-
      text exposure window.
    * Each tester runs under a hard :data:`_TESTER_TIMEOUT_SECONDS`
      wall-clock cap.  An unreachable Plex used to pin the request
      thread for 35 s through stacked timeouts; that's a self-inflicted
      DoS vector.
    """
    tester = _SERVICE_TESTERS.get(service)
    if tester is None:
        # Use the canonical envelope: ``error`` is the machine-readable
        # code, the human-readable name of the unknown service goes in
        # ``message`` so a frontend can surface it without parsing the
        # error code.
        return respond_err(
            "unknown_service",
            status=400,
            message=f"Unknown service: {service!r}",
        )

    cached = _cache_get(service)
    if cached is not None:
        return JSONResponse(cached)

    # Rate limit only the genuine tester invocation. Cache hits above
    # are cheap and must not consume budget — otherwise a normal page
    # reload exhausts the per-minute cap before the user does anything.
    if not _SETTINGS_TEST_LIMITER.check(admin):
        logger.warning("rate_limit.throttled scope=actor actor=%s", admin)
        return respond_err(
            "too_many_requests",
            status=429,
            message="Too many requests — slow down",
        )

    conn = get_db()
    config = request.app.state.config
    needed_keys = _SERVICE_TESTER_KEYS.get(service)
    try:
        settings = _load_settings(conn, config.secret_key, keys=needed_keys)
    except ConfigDecryptError as exc:
        logger.warning("Service test decrypt failed for %s key=%s", service, exc.key)
        return JSONResponse(
            {"ok": False, "error": f"decrypt_failed: {exc.key}"},
            status_code=200,
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(tester, settings)
            try:
                response = future.result(timeout=_TESTER_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "Service test exceeded %.0fs cap for %s — returning timeout",
                    _TESTER_TIMEOUT_SECONDS,
                    service,
                )
                payload = {"ok": False, "error": "timeout"}
                _cache_put(service, payload)
                return JSONResponse(payload)
    except Exception as exc:
        logger.warning("Service test failed for %s: %s", service, exc)
        return JSONResponse({"ok": False, "error": "Service connection test failed"})

    try:
        payload = json.loads(bytes(response.body).decode("utf-8"))
    except Exception:
        return response
    _cache_put(service, payload)
    return response


@router.get("/api/plex/libraries")
def api_plex_libraries(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all Plex library sections available on the configured server."""
    conn = get_db()
    config = request.app.state.config
    try:
        client = build_plex_from_db(conn, config.secret_key)
        if client is None:
            return respond_err(
                "plex_not_configured",
                status=200,
                message="Plex URL and token are not configured",
                libraries=[],
            )
        libraries = client.get_libraries()
        return JSONResponse({"libraries": libraries})
    except Exception as exc:
        logger.warning("Failed to fetch Plex libraries: %s", exc)
        return respond_err(
            "fetch_failed", status=200, message="Failed to fetch Plex libraries", libraries=[]
        )


@router.get("/api/settings/disk-usage")
def api_disk_usage(
    request: Request, path: str = "", admin: str = Depends(get_current_admin)
) -> JSONResponse:
    """Return disk usage stats for a whitelisted filesystem path."""
    from mediaman.services.infra.path_safety import disk_usage_allowed_roots, resolve_safe_path

    if not path:
        return respond_err("path_required", status=400, message="path parameter is required")
    if len(path) > 4096:
        return respond_err("path_too_long", status=400)

    roots = disk_usage_allowed_roots()
    resolved = resolve_safe_path(path, roots)
    if resolved is None:
        return respond_err("not_found", status=404)

    try:
        usage = shutil.disk_usage(str(resolved))
        total = usage.total
        used = usage.used
        pct = round(used / total * 100, 1) if total > 0 else 0.0
        return JSONResponse(
            {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": usage.free,
                "usage_pct": pct,
            }
        )
    except FileNotFoundError:
        return respond_err("not_found", status=404)
    except Exception as exc:
        logger.warning("Failed to read disk usage for %s: %s", resolved, exc)
        return respond_err("fetch_failed", message="Failed to read disk usage")
