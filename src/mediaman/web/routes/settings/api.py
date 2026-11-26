"""JSON API endpoints for settings: read, write, service tests, Plex libraries and disk usage,
plus their private helpers (write throttling, reauth gating, persistence, tester dispatch,
timeout and caching). The package barrel includes this router; tests patch symbols on the
owning module.
"""

# rationale: not split further. This is one concept — the settings JSON API — and its
# only natural seam, the four-stage tester pipeline (_resolve_tester_dispatch
# → _load_tester_settings → _run_tester_with_timeout → _cache_tester_response),
# is a single sequential chain bound to api_test_service; carving it into a
# sibling would scatter one flow across two files and break the
# ``settings.api`` monkeypatch surface the tests rely on for no navigability
# gain.

from __future__ import annotations

import concurrent.futures
import json
import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable
from typing import Annotated

import requests as _requests
from defusedxml.ElementTree import ParseError
from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import JSONResponse
from plexapi.exceptions import PlexApiException
from pydantic import Field
from starlette.responses import Response

from mediaman.bootstrap.scan_jobs import SCHEDULE_SETTING_KEYS, reschedule_scan_jobs
from mediaman.core.audit import security_event
from mediaman.core.time import now_iso
from mediaman.db import get_db
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import build_plex_from_db
from mediaman.services.infra import (
    ConfigDecryptError,
    SafeHTTPError,
    SSRFRefused,
    disk_usage_allowed_roots,
    resolve_safe_readonly_path,
)
from mediaman.services.infra.settings_reader import get_setting
from mediaman.services.infra.storage.disk_reads import settings_disk_reads
from mediaman.services.openai.client import key_refused
from mediaman.services.rate_limit import get_client_ip
from mediaman.services.rate_limit.instances import (
    SETTINGS_AUTO_TEST_LIMITER as _SETTINGS_AUTO_TEST_LIMITER,
)
from mediaman.services.rate_limit.instances import SETTINGS_TEST_LIMITER as _SETTINGS_TEST_LIMITER
from mediaman.services.rate_limit.instances import (
    SETTINGS_UNTICKETED_LIMITER as _SETTINGS_UNTICKETED_LIMITER,
)
from mediaman.services.rate_limit.instances import SETTINGS_WRITE_LIMITER as _SETTINGS_WRITE_LIMITER
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_admin
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
from mediaman.web.responses import respond_err, respond_failure, respond_ok
from mediaman.web.routes.settings.core import (
    validate_url_fields as _validate_url_fields,
)
from mediaman.web.routes.settings.secrets import (
    ALL_KEYS as _ALL_KEYS,
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
from mediaman.web.routes.settings.testers import (
    _TIMED_OUT,
    _failure,
    is_not_plex_reply,
)
from mediaman.web.routes.settings.testers import (
    SERVICE_TESTER_KEYS as _SERVICE_TESTER_KEYS,
)
from mediaman.web.routes.settings.testers import (
    TESTER_TIMEOUT_SECONDS as _TESTER_TIMEOUT_SECONDS,
)
from mediaman.web.routes.settings.testers import (
    TESTERS as _SERVICE_TESTERS,
)
from mediaman.web.routes.settings.testers import (
    cache_generation as _cache_generation,
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
from mediaman.web.routes.users.rate_limits import too_many

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/settings")
def api_get_settings(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all settings as JSON with secret fields masked as '****'. Secrets are never
    decrypted: the encrypted=1 flag is enough to mask them, and decrypting would only add an
    exposure window on every settings GET.
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
        return respond_err(
            "settings_decrypt_failed",
            status=500,
            message="Saved secrets couldn't be read. Check the secret key.",
        )
    settings = _mask_encrypted_keys(plain, enc_keys)
    return JSONResponse(settings)


def _needs_reauth(
    conn: sqlite3.Connection, body_dict: dict[str, object], session_token: str | None, admin: str
) -> bool:
    """Return whether the save changes a sensitive key without a recent reauth ticket. One
    sensitive key is enough, so mixing it with harmless fields cannot get a partial write.
    """
    return _touches_sensitive_keys(body_dict) and not has_recent_reauth(conn, session_token, admin)


def _reserve_settings_write(
    request: Request,
    conn: sqlite3.Connection,
    admin: str,
    body_dict: dict[str, object],
    *,
    needs_reauth: bool,
) -> float | JSONResponse:
    """Count one save against the admin's write budget and return the time it is counted at,
    or a 429 response once that is exhausted. A settings.write.throttled audit event is
    recorded first so a hit cap is visible in the log. A save that *needs_reauth* has a
    budget of its own as well.
    """
    limiter = _SETTINGS_WRITE_LIMITER
    if needs_reauth and not _SETTINGS_UNTICKETED_LIMITER.check(admin):
        limiter = _SETTINGS_UNTICKETED_LIMITER
    else:
        reserved = limiter.reserve(admin)
        if reserved is not None:
            return reserved
    logger.warning("settings.write_throttled user=%s", _sanitise_log_field(admin))
    security_event(
        conn,
        event="settings.write.throttled",
        actor=admin,
        ip=get_client_ip(request),
        detail={"keys": sorted(k for k in body_dict if k in _ALL_KEYS)},
    )
    return too_many(limiter, admin, "Too many settings changes.")


def _settings_reauth_required(admin: str) -> JSONResponse:
    logger.warning(
        "settings.write_rejected user=%s reason=reauth_required", _sanitise_log_field(admin)
    )
    return respond_err(
        "reauth_required",
        status=403,
        message="Recent password re-authentication required for sensitive settings",
        reauth_required=True,
    )


def _persist_settings(
    request: Request,
    conn: sqlite3.Connection,
    body_dict: dict[str, object],
    secret_key: str,
    admin: str,
    now: str,
) -> Response:
    """Write the settings rows and audit entry; return the saved/ignored envelope, with the
    libraries and thresholds as they are now stored, which may be more than was sent.
    Encrypt-on-write happens inside the repository so plaintext never escapes it, and the
    audit row shares the BEGIN IMMEDIATE so a failure rolls every mutation back together.
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
        logger.exception("settings.write failed user=%s", _sanitise_log_field(admin))
        return respond_err(
            "internal_error", status=500, message="Internal error during settings write"
        )
    _invalidate_test_cache_for_keys(set(written))
    if SCHEDULE_SETTING_KEYS.intersection(written):
        try:
            reschedule_scan_jobs(conn, getattr(request.app.state, "db_path", None), secret_key)
        # rationale: the save is already committed and the scheduler reads it at the next restart.
        except Exception:
            logger.warning("settings.reschedule_failed", exc_info=True)
    return respond_ok(
        {
            "status": "saved",
            "written": written,
            "ignored": ignored,
            "plex_libraries": get_setting(conn, "plex_libraries", default=[]),
            "disk_thresholds": get_setting(conn, "disk_thresholds", default={}),
        }
    )


#: Held while the libraries and thresholds are read, merged and written, so two saves cannot undo each other's.
_LIBRARY_MERGE_LOCK = threading.Lock()


class _SettingsPut(SettingsUpdate):
    """A settings save, with the library selection its ``plex_libraries`` was changed from."""

    plex_libraries_base: Annotated[list[str] | None, Field(max_length=128)] = None


def _merge_libraries(conn: sqlite3.Connection, sent: list[str], base: list[str]) -> list[str]:
    """Return the stored selection with what *sent* adds to and takes from *base* done to it.

    A page opened before another one's save still shows the old selection; sent whole, it
    would select again, and so scan, a library someone has since taken out.
    """
    stored = get_setting(conn, "plex_libraries", default=[])
    current = [str(v) for v in stored] if isinstance(stored, list) else []
    kept = [v for v in current if v in sent or v not in base]
    return kept + [v for v in sent if v not in base and v not in kept]


def _merge_disk_thresholds(
    conn: sqlite3.Connection, sent: dict[str, object]
) -> dict[str, object] | None:
    """Return the stored thresholds with *sent* laid over them, or None when nothing was sent.

    A library without a threshold is always scanned, so one that was not sent keeps its
    own; only an entry sent with neither a path nor a threshold removes it.
    """
    if not sent:
        return None
    stored = get_setting(conn, "disk_thresholds", default={})
    merged: dict[str, object] = dict(stored) if isinstance(stored, dict) else {}
    for library, cfg in sent.items():
        if isinstance(cfg, dict) and (cfg.get("path") or cfg.get("threshold")):
            merged[library] = cfg
        else:
            merged.pop(library, None)
    # A setting the scanner cannot read keeps every library from being scanned; clearing nothing must not mend it.
    return merged if merged or isinstance(stored, dict) else None


def _leaves_a_library_unlimited(
    conn: sqlite3.Connection, sent: dict[str, object], selected: object
) -> bool:
    """Return whether saving *sent* mends unreadable thresholds and leaves a selected library with no limit.

    While they cannot be read no library is scanned; once they can, one with no limit always is.
    """
    stored = get_setting(conn, "disk_thresholds", default={})
    if not stored or isinstance(stored, dict):
        return False
    entries = {library: cfg for library, cfg in sent.items() if isinstance(cfg, dict)}
    if not any(cfg.get("path") or cfg.get("threshold") for cfg in entries.values()):
        return False
    limited = {
        library
        for library, cfg in entries.items()
        if str(cfg.get("path") or "").strip() and int(cfg.get("threshold") or 0) > 0
    }
    return isinstance(selected, list) and not limited.issuperset(str(v) for v in selected)


@router.put("/api/settings")
def api_update_settings(
    request: Request,
    body: _SettingsPut,
    admin: str = Depends(get_current_admin),
    session_token: str | None = Cookie(default=None),
) -> Response:
    """Persist settings from the request body.

    Sensitive keys (every secret, URL field, mail address, base_url and deletion setting) need
    a recent-reauth ticket; see SENSITIVE_KEYS and _touches_sensitive_keys. Without it the
    entire PUT is rejected with 403, even if only some keys are sensitive. The write and audit row share
    one BEGIN IMMEDIATE transaction via security_event_or_raise, so a high-impact change
    never lacks an audit trail.
    """
    body_dict: dict[str, object] = body.model_dump(
        exclude_none=True, exclude={"plex_libraries_base"}
    )
    conn = get_db()
    needs_reauth = _needs_reauth(conn, body_dict, session_token, admin)
    reserved = _reserve_settings_write(request, conn, admin, body_dict, needs_reauth=needs_reauth)
    if isinstance(reserved, JSONResponse):
        return reserved

    if needs_reauth:
        # The page asks for the password and sends the save again, which is the one that counts.
        _SETTINGS_WRITE_LIMITER.release(admin, reserved)
        return _settings_reauth_required(admin)

    url_err = _validate_url_fields(body_dict)
    if url_err is not None:
        return url_err

    config = request.app.state.config
    with _LIBRARY_MERGE_LOCK:
        if body.plex_libraries is not None and body.plex_libraries_base is not None:
            body_dict["plex_libraries"] = _merge_libraries(
                conn, body.plex_libraries, body.plex_libraries_base
            )
        if body.disk_thresholds is not None:
            selected = body_dict.get("plex_libraries")
            if selected is None:
                selected = get_setting(conn, "plex_libraries", default=[])
            if _leaves_a_library_unlimited(conn, body.disk_thresholds, selected):
                return respond_err(
                    "disk_thresholds_unreadable",
                    status=409,
                    message="The stored disk thresholds can't be read, so scheduled scans skip every library. "
                    "Give every selected library a path and a Scan above limit, then save.",
                )
            thresholds = _merge_disk_thresholds(conn, body.disk_thresholds)
            if thresholds is None:
                del body_dict["disk_thresholds"]
            else:
                body_dict["disk_thresholds"] = thresholds
        return _persist_settings(request, conn, body_dict, config.secret_key, admin, now_iso())


def _resolve_tester_dispatch(
    service: str, admin: str, *, auto: bool
) -> tuple[Callable[[dict[str, object]], JSONResponse] | None, JSONResponse | None]:
    """Look up service's tester and gate cache and rate-limit replies. Returns (tester, None)
    to proceed with a fresh run, or (None, response) for unknown-service, cache-hit and
    rate-limit replies. Cache hits bypass the limiter so a settings page reload does not eat
    the per-minute budget, and a check the page makes by itself (*auto*) has a budget of its own.
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
        payload, status_code = cached
        return None, JSONResponse(payload, status_code=status_code)
    limiter = _SETTINGS_AUTO_TEST_LIMITER if auto else _SETTINGS_TEST_LIMITER
    if not limiter.check(admin):
        logger.warning("rate_limit.throttled scope=actor actor=%s", _sanitise_log_field(admin))
        return None, too_many(limiter, admin, "Too many tests.")
    return tester, None


def _load_tester_settings(
    service: str, conn: sqlite3.Connection, secret_key: str
) -> tuple[dict[str, object] | None, JSONResponse | None]:
    """Load only the settings keys service's tester needs (see _SERVICE_TESTER_KEYS), so
    testing one service does not decrypt every secret. Returns (settings, None) or (None,
    error_response) when decryption fails.
    """
    needed_keys = _SERVICE_TESTER_KEYS.get(service)
    try:
        return _load_settings(conn, secret_key, keys=needed_keys), None
    except ConfigDecryptError as exc:
        logger.warning("Service test decrypt failed for %s key=%s", service, exc.key)
        return None, _failure(
            f"decrypt_failed: {exc.key}", "The saved key couldn't be read. Save it again."
        )


def _run_tester_with_timeout(
    service: str,
    tester: Callable[[dict[str, object]], JSONResponse],
    settings: dict[str, object],
) -> JSONResponse:
    """Run tester under a hard _TESTER_TIMEOUT_SECONDS cap: an unreachable Plex once pinned the
    request thread for 35 s through stacked timeouts. The response is returned verbatim so
    the caller can cache the body and keep the original HTTP envelope.
    """
    # No ``with`` block: its exit waits for the hung thread. A thread that outlives the cap
    # ends when the tester's own HTTP timeout fires; the per-admin rate limits on the route
    # bound how many can be started meanwhile.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(tester, settings)
        try:
            return future.result(timeout=_TESTER_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Service test exceeded %.0fs cap for %s — returning timeout",
                _TESTER_TIMEOUT_SECONDS,
                service,
            )
            return _failure("timeout", _TIMED_OUT)
    except Exception:  # rationale: dispatch fanout for arbitrary tester callables; a single broken tester must not surface as a 500 on the settings page.
        logger.exception("Service test failed for %s", service)
        return respond_failure("Service connection test failed", status=200)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _cache_tester_response(service: str, response: JSONResponse, generation: int) -> JSONResponse:
    """Decode the tester response body and cache the JSON payload. A non-JSON body returns the
    raw response unchanged, preserving its status code and headers.
    """
    try:
        payload = json.loads(bytes(response.body).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return response
    if isinstance(payload, dict):
        _cache_put(service, payload, response.status_code, generation=generation)
    return response


@router.post("/api/settings/test/{service}")
def api_test_service(
    service: str,
    request: Request,
    auto: bool = False,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Test connectivity for a named service using the stored settings.

    One endpoint dispatches through _SERVICE_TESTERS (service name to tester); unknown names
    return 400. Layered on top:

    * A per-admin rate limit (10/min, 60/day) stops a logged-in attacker chaining tests to
      flood Plex or Mailgun. The page's own checks send ``auto`` and have their own
      (40/min, 400/day). Both guard real invocations only; cache-served replies bypass them.
    * Decryption is limited to the keys the tester needs (see _SERVICE_TESTER_KEYS),
      avoiding a plaintext exposure window for every secret.
    * Each tester runs under a hard _TESTER_TIMEOUT_SECONDS cap.
    """
    conn = get_db()
    config = request.app.state.config
    if service == "openai" and key_refused(conn, config.secret_key):
        # A passing result kept from before the refusal must not hide it.
        _invalidate_test_cache_for_keys({"openai_api_key"})
    tester, short_circuit = _resolve_tester_dispatch(service, admin, auto=auto)
    if short_circuit is not None:
        return short_circuit
    if tester is None:  # _resolve_tester_dispatch guarantees one or the other
        raise RuntimeError(
            f"tester is None for service={service!r} after dispatch — invariant violated"
        )

    generation = _cache_generation(service)
    settings, decrypt_err = _load_tester_settings(service, conn, config.secret_key)
    if decrypt_err is not None:
        return decrypt_err
    if settings is None:  # _load_tester_settings guarantees one or the other
        raise RuntimeError(
            f"settings is None for service={service!r} after decrypt — invariant violated"
        )

    response = _run_tester_with_timeout(service, tester, settings)
    return _cache_tester_response(service, response, generation)


@router.get("/api/plex/libraries")
def api_plex_libraries(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    conn = get_db()
    config = request.app.state.config
    try:
        client = build_plex_from_db(conn, config.secret_key)
        if client is None:
            return respond_err(
                "plex_not_configured",
                status=200,
                message="Plex URL and token are not configured.",
                libraries=[],
            )
        libraries = client.get_libraries()
        return JSONResponse({"libraries": libraries})
    except (
        SafeHTTPError,
        SSRFRefused,
        # rationale: PlexClient uses plexapi which maintains its own requests
        # session; ``get_libraries`` calls ``server.library.sections()``
        # which does not route through SafeHTTPClient, so bare
        # RequestException can escape (confirmed in plex.py:334,368).
        # Removing this catch would surface transport errors as 500s.
        _requests.RequestException,
        ArrError,
        ConfigDecryptError,
        # a wrong or expired token surfaces as plexapi's Unauthorized
        PlexApiException,
        ValueError,
        # a reply that is not a Plex server's answer: only that one is handled below
        ParseError,
        TypeError,
        AttributeError,
    ) as exc:
        if isinstance(exc, (ParseError, TypeError, AttributeError)) and not is_not_plex_reply(exc):
            raise
        if isinstance(exc, (SafeHTTPError, _requests.RequestException, PlexApiException)) or (
            is_not_plex_reply(exc)
        ):
            # A wrong address, a refused token or a server that is down is an expected
            # state, not a fault: one line, no traceback.
            logger.warning("Failed to fetch Plex libraries: %s", type(exc).__name__)
        else:
            logger.exception("Failed to fetch Plex libraries")
        return respond_err(
            "fetch_failed", status=200, message="Failed to fetch Plex libraries.", libraries=[]
        )


_MISSING: dict[str, float] = {}
_NOT_ALLOWED: dict[str, float] = {}


def _read_safe_disk_usage(path: str) -> dict[str, float]:
    """Return the usage of *path*'s disk, or ``_NOT_ALLOWED`` or ``_MISSING`` in its place.

    What is not allowed is the same whether or not it exists: only a path under an allowed
    root may be told from a missing one.
    """
    resolved = resolve_safe_readonly_path(path, disk_usage_allowed_roots())
    if resolved is None:
        return _NOT_ALLOWED
    try:
        usage = shutil.disk_usage(str(resolved))
    except FileNotFoundError:
        return _MISSING
    except OSError:
        logger.exception("Failed to read disk usage for %s", resolved)
        raise
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "usage_pct": round(usage.used / usage.total * 100, 1) if usage.total > 0 else 0.0,
    }


@router.get("/api/settings/disk-usage")
def api_disk_usage(
    request: Request, path: str = "", admin: str = Depends(get_current_admin)
) -> JSONResponse:
    if not path:
        return respond_err("path_required", status=400, message="path parameter is required.")
    if len(path) > 4096:
        return respond_err("path_too_long", status=400)

    usage = settings_disk_reads.read(path, _read_safe_disk_usage)
    if usage is None:
        return respond_err("fetch_failed", message="Failed to read disk usage.")
    if usage is _NOT_ALLOWED:
        return respond_err("not_allowed", status=403, message="This page may not read this folder.")
    if usage is _MISSING:
        return respond_err("not_found", status=404, message="Folder not found.")
    return JSONResponse(usage)
