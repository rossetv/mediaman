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

# rationale: package barrel + 6 route handlers + ``_load_settings``; the
# ``_load_settings`` function is a monkeypatch target in tests so extracting
# it would break the test suite's patching contract; the route handlers share
# rate-limiter singletons and settings-loading state that make further
# decomposition incremental rather than immediately safe.

from __future__ import annotations

import concurrent.futures
import json
import logging
import shutil
import sqlite3
from collections.abc import Callable
from typing import cast

import requests
from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.audit import security_event
from mediaman.core.time import now_iso
from mediaman.db import get_db
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import build_plex_from_db
from mediaman.services.infra import ConfigDecryptError, SafeHTTPError
from mediaman.services.infra import (
    is_safe_outbound_url as is_safe_outbound_url,  # re-exported for patch targets
)
from mediaman.services.rate_limit import get_client_ip
from mediaman.services.rate_limit.instances import SETTINGS_TEST_LIMITER as _SETTINGS_TEST_LIMITER
from mediaman.services.rate_limit.instances import SETTINGS_WRITE_LIMITER as _SETTINGS_WRITE_LIMITER
from mediaman.web.auth.middleware import get_current_admin, resolve_page_session
from mediaman.web.auth.password_hash import get_user_email as _get_user_email
from mediaman.web.auth.reauth import has_recent_reauth
from mediaman.web.models import SettingsUpdate
from mediaman.web.repository.settings import (
    fetch_encrypted_key_set as _encrypted_keys,
)
from mediaman.web.repository.settings import (
    load_settings as _load_settings,
)
from mediaman.web.repository.settings import (
    write_settings,
)
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
    SECRET_FIELDS as SECRET_FIELDS,  # re-exported for test imports
)
from mediaman.web.routes.settings.secrets import (
    SENSITIVE_KEYS,
)
from mediaman.web.routes.settings.secrets import (
    has_sensitive_key_changes as _touches_sensitive_keys,
)
from mediaman.web.routes.settings.secrets import (
    mask_encrypted_keys as _mask_encrypted_keys,
)
from mediaman.web.routes.settings.secrets import (
    mask_secrets as _mask_secrets,  # noqa: F401 — kept for backward compat
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

logger = logging.getLogger(__name__)

router = APIRouter()


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

    self_email = _get_user_email(conn, username)

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "username": username,
            "email": self_email,
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


def _settings_write_throttled(
    request: Request, conn: sqlite3.Connection, admin: str, body_dict: dict[str, object]
) -> JSONResponse | None:
    """Return a 429 response if the admin has exhausted the write budget; else None.

    Records a ``settings.write.throttled`` audit event before refusing so
    we can see in the log when an actor is hitting the cap.
    """
    if _SETTINGS_WRITE_LIMITER.check(admin):
        return None
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


def _settings_reauth_required(
    conn: sqlite3.Connection, body_dict: dict[str, object], session_token: str | None, admin: str
) -> JSONResponse | None:
    """Return a 403 when a sensitive key change lacks a recent reauth ticket; else None.

    An attacker mixing one sensitive field with several harmless ones
    must not get a partial write, so a single sensitive key in the body
    rejects the entire PUT.
    """
    sensitive_write = _touches_sensitive_keys(body_dict)
    if sensitive_write and not has_recent_reauth(conn, session_token, admin):
        logger.warning("settings.write_rejected user=%s reason=reauth_required", admin)
        return respond_err(
            "reauth_required",
            status=403,
            message="Recent password re-authentication required for sensitive settings",
            reauth_required=True,
        )
    return None


def _persist_settings(
    request: Request,
    conn: sqlite3.Connection,
    body_dict: dict[str, object],
    secret_key: str,
    admin: str,
    now: str,
) -> Response:
    """Write the settings rows and audit entry; return the saved/ignored envelope.

    Encrypt-on-write happens inside the repository so the plaintext
    never escapes that boundary (§9.9). The audit row is written under
    the same ``BEGIN IMMEDIATE`` so a SQLite or audit failure rolls
    every settings mutation back together (M27).
    """
    written = sorted(k for k in body_dict if k in _ALL_KEYS)
    ignored = sorted(k for k in body_dict if k not in _ALL_KEYS)
    sensitive_written = sorted(k for k in written if k in SENSITIVE_KEYS)
    try:
        write_settings(
            conn,
            body_dict=body_dict,
            allowed_keys=_ALL_KEYS,
            secret_key=secret_key,
            now=now,
            audit={
                "event": "settings.write",
                "actor": admin,
                "ip": get_client_ip(request),
                "detail": {"keys": written, "sensitive_keys": sensitive_written},
            },
        )
    except sqlite3.Error:
        logger.exception("settings.write failed user=%s", admin)
        return respond_err(
            "internal_error", status=500, message="Internal error during settings write"
        )
    _invalidate_test_cache_for_keys(set(written))
    return respond_ok({"status": "saved", "written": written, "ignored": ignored})


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
    body_dict: dict[str, object] = body.model_dump(exclude_none=True)
    conn = get_db()
    throttled = _settings_write_throttled(request, conn, admin, body_dict)
    if throttled is not None:
        return throttled

    url_err = _validate_url_fields(body_dict)
    if url_err is not None:
        return url_err

    reauth_err = _settings_reauth_required(conn, body_dict, session_token, admin)
    if reauth_err is not None:
        return reauth_err

    config = request.app.state.config
    return _persist_settings(request, conn, body_dict, config.secret_key, admin, now_iso())


def _resolve_tester_dispatch(
    service: str, admin: str
) -> tuple[Callable[[dict[str, object]], JSONResponse] | None, JSONResponse | None]:
    """Look up *service*'s tester and gate cache/rate-limit replies.

    Returns ``(tester, None)`` when the caller should proceed to a fresh
    invocation, ``(None, response)`` for unknown-service, cache-hit, and
    rate-limit replies. Cache hits bypass the rate limiter so a settings
    page reload (8 services × cache hit) does not eat the per-minute
    budget on the second visit.
    """
    tester = _SERVICE_TESTERS.get(service)
    if tester is None:
        # Use the canonical envelope: ``error`` is the machine-readable
        # code, the human-readable name of the unknown service goes in
        # ``message`` so a frontend can surface it without parsing the
        # error code.
        return None, respond_err(
            "unknown_service",
            status=400,
            message=f"Unknown service: {service!r}",
        )
    cached = _cache_get(service)
    if cached is not None:
        return None, JSONResponse(cached)
    if not _SETTINGS_TEST_LIMITER.check(admin):
        logger.warning("rate_limit.throttled scope=actor actor=%s", admin)
        return None, respond_err(
            "too_many_requests",
            status=429,
            message="Too many requests — slow down",
        )
    return tester, None


def _load_tester_settings(
    service: str, conn: sqlite3.Connection, secret_key: str
) -> tuple[dict[str, object] | None, JSONResponse | None]:
    """Load only the settings keys *service*'s tester actually needs.

    Returns ``(settings, None)`` on success or ``(None, error_response)``
    when decryption fails. Restricting decryption to the named keys
    (see :data:`_SERVICE_TESTER_KEYS`) avoids decrypting every secret in
    the DB just to test one of them.
    """
    needed_keys = _SERVICE_TESTER_KEYS.get(service)
    try:
        return _load_settings(conn, secret_key, keys=needed_keys), None
    except ConfigDecryptError as exc:
        logger.warning("Service test decrypt failed for %s key=%s", service, exc.key)
        return None, JSONResponse(
            {"ok": False, "error": f"decrypt_failed: {exc.key}"},
            status_code=200,
        )


def _run_tester_with_timeout(
    service: str,
    tester: Callable[[dict[str, object]], JSONResponse],
    settings: dict[str, object],
) -> JSONResponse:
    """Run *tester* under a hard :data:`_TESTER_TIMEOUT_SECONDS` cap.

    An unreachable Plex used to pin the request thread for 35 s through
    stacked timeouts; the wall-clock cap prevents that self-inflicted
    DoS vector. The tester response is returned verbatim so the caller
    can cache the JSON body and surface the original HTTP envelope.
    """
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(tester, settings)
            try:
                return future.result(timeout=_TESTER_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "Service test exceeded %.0fs cap for %s — returning timeout",
                    _TESTER_TIMEOUT_SECONDS,
                    service,
                )
                payload = {"ok": False, "error": "timeout"}
                _cache_put(service, payload)
                return JSONResponse(payload)
    except Exception:  # rationale: §6.4 site 2 — dispatch fanout for arbitrary tester callables; a single broken tester must not surface as a 500 on the settings page.
        logger.exception("Service test failed for %s", service)
        return JSONResponse({"ok": False, "error": "Service connection test failed"})


def _cache_tester_response(service: str, response: JSONResponse) -> JSONResponse:
    """Decode the tester response body and cache the JSON payload.

    Falls through to returning the raw response unchanged when the body
    is not a JSON envelope — preserves the original status code and
    headers for non-cacheable replies (e.g. a streamed timeout payload
    already cached upstream).
    """
    try:
        payload = json.loads(bytes(response.body).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return response
    _cache_put(service, payload)
    return response


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
    tester, short_circuit = _resolve_tester_dispatch(service, admin)
    if short_circuit is not None:
        return short_circuit
    assert tester is not None  # _resolve_tester_dispatch guarantees one or the other

    conn = get_db()
    config = request.app.state.config
    settings, decrypt_err = _load_tester_settings(service, conn, config.secret_key)
    if decrypt_err is not None:
        return decrypt_err
    assert settings is not None  # _load_tester_settings guarantees one or the other

    response = _run_tester_with_timeout(service, tester, settings)
    return _cache_tester_response(service, response)


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
    except (
        SafeHTTPError,
        requests.RequestException,
        ArrError,
        ConfigDecryptError,
        ValueError,
    ):
        logger.exception("Failed to fetch Plex libraries")
        return respond_err(
            "fetch_failed", status=200, message="Failed to fetch Plex libraries", libraries=[]
        )


@router.get("/api/settings/disk-usage")
def api_disk_usage(
    request: Request, path: str = "", admin: str = Depends(get_current_admin)
) -> JSONResponse:
    """Return disk usage stats for a whitelisted filesystem path."""
    from mediaman.services.infra import disk_usage_allowed_roots, resolve_safe_path

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
    except OSError:
        logger.exception("Failed to read disk usage for %s", resolved)
        return respond_err("fetch_failed", message="Failed to read disk usage")
