"""Settings routes."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse as _urlparse

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.audit import security_event, security_event_or_raise
from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.auth.rate_limit import get_client_ip
from mediaman.auth.reauth import has_recent_reauth
from mediaman.crypto import decrypt_value, encrypt_value
from mediaman.db import get_db
from mediaman.services.arr.build import build_plex_from_db
from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError
from mediaman.services.infra.path_safety import disk_usage_allowed_roots, resolve_safe_path
from mediaman.services.infra.rate_limits import (
    SETTINGS_TEST_LIMITER as _SETTINGS_TEST_LIMITER,
)
from mediaman.services.infra.rate_limits import (
    SETTINGS_WRITE_LIMITER as _SETTINGS_WRITE_LIMITER,
)
from mediaman.services.infra.settings_reader import ConfigDecryptError
from mediaman.services.infra.time import now_iso
from mediaman.services.infra.url_safety import is_safe_outbound_url
from mediaman.web.models import _API_KEY_RE, SettingsUpdate
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger("mediaman")

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentinel value displayed in the UI and sent back when a secret field is
#: unchanged — never persisted to the database.
_SECRET_PLACEHOLDER = "****"

#: Explicit "delete this row" sentinel for secret fields. The previous
#: design conflated "" (no-op) with "clear" — once a secret was stored,
#: the UI had no way to delete it without falling back to direct DB
#: surgery. Sending this sentinel deletes the row.
_SECRET_CLEAR_SENTINEL = "__CLEAR__"

#: Per-tester upper bound on time spent in a single tester() call. An
#: unreachable Plex / Sonarr / mailgun must not pin the request thread
#: indefinitely — without this cap, a 35 s underlying timeout was
#: observable when chained with retries and reverse-proxy buffering.
_TESTER_TIMEOUT_SECONDS = 15.0

#: OpenAI models endpoint used by the connectivity test.
_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"

#: TMDB configuration endpoint used by the connectivity test.
_TMDB_CONFIG_URL = "https://api.themoviedb.org/3/configuration"

#: OMDb root endpoint used by the connectivity test.
_OMDB_TEST_URL = "https://www.omdbapi.com/"

SECRET_FIELDS = {
    "plex_token",
    "sonarr_api_key",
    "radarr_api_key",
    "nzbget_password",
    "mailgun_api_key",
    "tmdb_api_key",
    "tmdb_read_token",
    "openai_api_key",
    "omdb_api_key",
}

# Note: SENSITIVE_KEYS (declared further down) is unioned with SECRET_FIELDS
# at the bottom of the module so we never need to remember to keep both
# membership tests in sync.

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
    "auto_abandon_enabled",
}

#: Internal crypto plumbing rows (HKDF salt, canary) — never shown in the UI.
_INTERNAL_KEYS = {"aes_kdf_salt", "aes_kdf_canary"}

#: Settings keys that require a recent-reauth ticket before they can be
#: written. Anything that touches outbound integrations, mail, base URL, or
#: data exfiltration vectors lives here. Anything that's purely a UI hint
#: (``scan_day``, ``scan_time``) does not — gating those would just train
#: operators to reauthenticate twice a day for low-impact tweaks.
#:
#: Membership rule: include the key when one of the following is true:
#:   * The value is a credential / API key / token (every key in
#:     :data:`SECRET_FIELDS`).
#:   * The value is a URL or hostname the server will fetch (``*_url``
#:     — Plex, Sonarr, Radarr, Mailgun's `mailgun_domain`, etc.).
#:   * The value influences how outbound mail is addressed
#:     (``mailgun_from_address``).
#:   * The value influences security headers / external link generation
#:     (``base_url`` — used in unsubscribe and keep-link emails).
SENSITIVE_KEYS = {
    "plex_url",
    "plex_public_url",
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
} | SECRET_FIELDS


def _touches_sensitive_keys(body: dict) -> bool:
    """Return True when *body* attempts to write any sensitive key.

    Secret fields whose value is the unchanged sentinel (``****``) or an
    empty string are skipped because the PUT handler ignores them too —
    a no-op write should not demand a fresh reauth. The explicit
    :data:`_SECRET_CLEAR_SENTINEL` is NOT skipped: deleting a stored
    credential is a sensitive change.
    """
    for key, value in body.items():
        if key not in SENSITIVE_KEYS:
            continue
        if key in SECRET_FIELDS and (value == _SECRET_PLACEHOLDER or value == ""):
            continue
        if value is None:
            continue
        return True
    return False


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
                # Distinguish "decrypt failed" from "value never set" —
                # see ConfigDecryptError docstring.
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


def _encrypted_keys(conn: sqlite3.Connection) -> set[str]:
    """Return the set of keys in the ``settings`` table that are stored encrypted.

    Used by the masking layer of GET /api/settings so we never pay the
    cost of decrypting a secret just to immediately mask it. The
    distinction "is this key encrypted on disk?" is enough — we don't
    need the plaintext.
    """
    return {
        row["key"] for row in conn.execute("SELECT key FROM settings WHERE encrypted=1").fetchall()
    }


def _mask_secrets(settings: dict[str, object]) -> dict[str, object]:
    """Return a copy of *settings* with secret fields replaced by '****'."""
    out = dict(settings)
    for key in SECRET_FIELDS:
        if out.get(key):
            out[key] = _SECRET_PLACEHOLDER
    return out


def _mask_encrypted_keys(
    settings: dict[str, object], encrypted_keys: set[str]
) -> dict[str, object]:
    """Return a copy of *settings* with every encrypted-on-disk key showing '****'.

    Unlike :func:`_mask_secrets`, this does not require the plaintext to
    have been read — the caller passes a pre-computed set of keys that
    are encrypted in the DB. Used by GET /api/settings to avoid
    decrypting secrets just to immediately throw the plaintext away.
    """
    out = dict(settings)
    for key in encrypted_keys & SECRET_FIELDS:
        out[key] = _SECRET_PLACEHOLDER
    return out


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
    encrypted_keys = _encrypted_keys(conn)
    config = request.app.state.config
    try:
        plain = _load_settings(
            conn,
            config.secret_key,
            keys=set(_ALL_KEYS) - encrypted_keys,
        )
    except ConfigDecryptError:
        # Should not happen — we filtered out encrypted rows. Defensive
        # only.
        plain = {}
    settings = _mask_encrypted_keys(plain, encrypted_keys)

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


@router.get("/api/settings")
def api_get_settings(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all settings as JSON with secret fields masked as '****'.

    Skips decryption of secret fields — we read the ``encrypted=1`` flag
    instead and emit ``****`` directly. The plaintext is never needed
    here and decrypting it just to mask it is wasted work + an
    unnecessary exposure window for every secret on every settings GET.
    """
    conn = get_db()
    config = request.app.state.config
    encrypted_keys = _encrypted_keys(conn)
    try:
        plain = _load_settings(
            conn,
            config.secret_key,
            keys=set(_ALL_KEYS) - encrypted_keys,
        )
    except ConfigDecryptError:
        # Defensive — we filtered encrypted keys out. If this ever
        # fires, surface a 500 rather than silently shipping partial
        # data to the UI.
        return respond_err("settings_decrypt_failed", status=500)
    settings = _mask_encrypted_keys(plain, encrypted_keys)
    return JSONResponse(settings)


_URL_FIELDS = frozenset(
    {
        "base_url",
        "plex_url",
        "plex_public_url",
        "sonarr_url",
        "sonarr_public_url",
        "radarr_url",
        "radarr_public_url",
        "nzbget_url",
        "nzbget_public_url",
    }
)


def _scrub_url_for_log(candidate: str) -> str:
    """Return a log-safe representation of *candidate* — host + path-prefix only.

    The SSRF-blocked path used to log the candidate URL verbatim. That is
    user-supplied content that may carry an embedded password
    (``http://admin:pa55w0rd@host``), an API key in the query string
    (``?api_key=sk-...``), or an attacker-tagged URL designed for the log
    viewer. We strip:

    * userinfo (anything before the ``@`` in the netloc),
    * the query string,
    * the fragment,
    * the path beyond the first 32 characters,

    then return ``scheme://host[:port]/<truncated path>``. If the URL
    fails to parse, fall back to a length-only marker so the log still
    has something useful for triage.
    """
    try:
        parsed = _urlparse(candidate)
    except (ValueError, TypeError):
        return f"<unparseable len={len(candidate)}>"
    scheme = (parsed.scheme or "").lower() or "?"
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = parsed.path or ""
    if len(path) > 32:
        path = path[:32] + "…"
    return f"{scheme}://{host}{port}{path}"


def _validate_url_fields(body: dict) -> JSONResponse | None:
    """Validate all URL fields in *body*.

    Returns a :class:`JSONResponse` error if any URL field is invalid
    (too long, wrong scheme, or blocked by the SSRF guard), or ``None``
    if all URL fields pass validation.
    """
    for url_key in _URL_FIELDS:
        if body.get(url_key):
            candidate = str(body[url_key]).strip()
            if len(candidate) > 2048:
                return respond_err("url_too_long", status=400, message=f"{url_key} too long")
            try:
                parsed = _urlparse(candidate)
            except ValueError:
                parsed = None
            if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc:
                return respond_err(
                    "invalid_url",
                    status=400,
                    message=f"{url_key} must be an http(s) URL",
                )
            if not is_safe_outbound_url(candidate):
                logger.warning(
                    "settings.ssrf_blocked key=%s value=%s",
                    url_key,
                    _scrub_url_for_log(candidate),
                )
                return respond_err(
                    "ssrf_blocked",
                    status=400,
                    message=f"{url_key} points at a blocked address",
                )
    return None


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
        # Pair the throttle log with an audit row — without it,
        # operators reviewing a brute-force attempt see the limiter
        # firing in app logs but no trail in the audit_log.
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
                        # Explicit "delete this row" — clears a stored
                        # secret entirely. Without this sentinel the UI
                        # had no way to reverse a credential save short
                        # of direct DB surgery.
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

            # Audit-in-transaction: if the audit insert blows up, the
            # whole settings write rolls back. Fail closed for the keys
            # that can leak data or punch through SSRF guards if changed
            # silently.
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


def _safe_http_error_to_response(exc: SafeHTTPError) -> JSONResponse:
    """Convert a :class:`SafeHTTPError` to a standard test-result JSONResponse.

    Handles the three recurring SafeHTTPError shapes that all service tests
    share: SSRF refusal, transport errors (timeout / connection refused), and
    HTTP auth failures.  All other status codes fall through to a generic message.
    """
    if exc.status_code == 0:
        snippet = exc.body_snippet
        if "refused by SSRF" in snippet:
            return JSONResponse({"ok": False, "error": "ssrf_refused"})
        if "transport error" in snippet:
            kind = "timeout" if "timeout" in snippet.lower() else "connection_refused"
            return JSONResponse({"ok": False, "error": kind})
    if exc.status_code in (401, 403):
        return JSONResponse({"ok": False, "error": "auth_failed"})
    return JSONResponse({"ok": False, "error": f"other: HTTP {exc.status_code}"})


def _test_bearer_api(url: str, api_key: str) -> JSONResponse:
    """Test a Bearer-token-authenticated API endpoint.

    Used by the OpenAI and TMDB service tests, which share identical
    request / error-handling logic.  Returns a JSONResponse; never raises.
    """
    try:
        SafeHTTPClient().get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(5.0, 15.0),
        )
        return JSONResponse({"ok": True})
    except SafeHTTPError as exc:
        return _safe_http_error_to_response(exc)


def _test_plex(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.media_meta.plex import PlexClient

    url = str(settings.get("plex_url") or "")
    token = str(settings.get("plex_token") or "")
    if not url or not token:
        return JSONResponse({"ok": False, "error": "Plex URL and token are required"})
    PlexClient(url, token).get_libraries()
    return JSONResponse({"ok": True})


def _test_sonarr(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.arr.sonarr import SonarrClient

    url = str(settings.get("sonarr_url") or "")
    api_key = str(settings.get("sonarr_api_key") or "")
    if not url or not api_key:
        return JSONResponse({"ok": False, "error": "Sonarr URL and API key are required"})
    ok = SonarrClient(url, api_key).test_connection()
    return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})


def _test_radarr(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.arr.radarr import RadarrClient

    url = str(settings.get("radarr_url") or "")
    api_key = str(settings.get("radarr_api_key") or "")
    if not url or not api_key:
        return JSONResponse({"ok": False, "error": "Radarr URL and API key are required"})
    ok = RadarrClient(url, api_key).test_connection()
    return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})


def _test_nzbget(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.downloads.nzbget import NzbgetClient

    url = str(settings.get("nzbget_url") or "")
    username = str(settings.get("nzbget_username") or "")
    password = str(settings.get("nzbget_password") or "")
    if not url:
        return JSONResponse({"ok": False, "error": "NZBGet URL is required"})
    ok = NzbgetClient(url, username, password).test_connection()
    return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})


def _test_mailgun(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.mail.mailgun import MailgunClient

    domain = str(settings.get("mailgun_domain") or "")
    api_key = str(settings.get("mailgun_api_key") or "")
    from_address = str(settings.get("mailgun_from_address") or "")
    if not domain or not api_key:
        return JSONResponse({"ok": False, "error": "Mailgun domain and API key are required"})
    ok = MailgunClient(domain, api_key, from_address).test_connection()
    return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})


def _test_openai(settings: dict[str, object]) -> JSONResponse:
    api_key = str(settings.get("openai_api_key") or "")
    if not api_key:
        return JSONResponse({"ok": False, "error": "OpenAI API key is required"})
    if not _API_KEY_RE.match(api_key):
        return JSONResponse(
            {"ok": False, "error": "auth_failed: API key contains invalid characters"}
        )
    return _test_bearer_api(_OPENAI_MODELS_URL, api_key)


def _test_tmdb(settings: dict[str, object]) -> JSONResponse:
    read_token = str(settings.get("tmdb_read_token") or "")
    if not read_token:
        return JSONResponse({"ok": False, "error": "TMDB Read Token is required"})
    if not _API_KEY_RE.match(read_token):
        return JSONResponse(
            {"ok": False, "error": "auth_failed: token contains invalid characters"}
        )
    return _test_bearer_api(_TMDB_CONFIG_URL, read_token)


def _test_omdb(settings: dict[str, object]) -> JSONResponse:
    api_key = str(settings.get("omdb_api_key") or "")
    if not api_key:
        return JSONResponse({"ok": False, "error": "OMDB API key is required"})
    if not _API_KEY_RE.match(api_key):
        return JSONResponse(
            {"ok": False, "error": "auth_failed: API key contains invalid characters"}
        )
    try:
        resp = SafeHTTPClient().get(
            _OMDB_TEST_URL,
            params={"apikey": api_key, "i": "tt0111161"},
            timeout=(5.0, 15.0),
        )
        data = resp.json()
        if data.get("Response") == "True":
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": data.get("Error", "auth_failed")})
    except SafeHTTPError as exc:
        return _safe_http_error_to_response(exc)


#: Dispatch table mapping service name → per-service test function.
_SERVICE_TESTERS: dict[str, Callable[[dict[str, object]], JSONResponse]] = {
    "plex": _test_plex,
    "sonarr": _test_sonarr,
    "radarr": _test_radarr,
    "nzbget": _test_nzbget,
    "mailgun": _test_mailgun,
    "openai": _test_openai,
    "tmdb": _test_tmdb,
    "omdb": _test_omdb,
}


#: Per-tester key allow-list. Each tester only needs a small subset of
#: settings — restricting :func:`_load_settings` to that subset means
#: triggering one tester does NOT decrypt every other secret in the DB.
_SERVICE_TESTER_KEYS: dict[str, set[str]] = {
    "plex": {"plex_url", "plex_token"},
    "sonarr": {"sonarr_url", "sonarr_api_key"},
    "radarr": {"radarr_url", "radarr_api_key"},
    "nzbget": {"nzbget_url", "nzbget_username", "nzbget_password"},
    "mailgun": {"mailgun_domain", "mailgun_api_key", "mailgun_from_address"},
    "openai": {"openai_api_key"},
    "tmdb": {"tmdb_read_token"},
    "omdb": {"omdb_api_key"},
}


#: TTL for the service-test result cache. The settings page auto-fires
#: a test for every configured service on load, which trivially blows
#: the per-admin rate limit (10/min) on a couple of reloads. Caching
#: the result for 120s makes reloads cheap without hiding genuine
#: connectivity changes for long.
_TEST_CACHE_TTL_SECONDS = 120.0

#: In-memory cache of the most recent tester payload per service.
#: Shared across admins because the underlying settings are global.
#: Invalidated on any settings write that touches the service's keys.
_TEST_CACHE: dict[str, tuple[float, dict]] = {}
_TEST_CACHE_LOCK = threading.Lock()


def _cache_get(service: str) -> dict | None:
    with _TEST_CACHE_LOCK:
        entry = _TEST_CACHE.get(service)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            _TEST_CACHE.pop(service, None)
            return None
        return payload


def _cache_put(service: str, payload: dict) -> None:
    with _TEST_CACHE_LOCK:
        _TEST_CACHE[service] = (time.monotonic() + _TEST_CACHE_TTL_SECONDS, payload)


def _invalidate_test_cache_for_keys(written_keys: set[str]) -> None:
    """Drop cached test results for any service whose inputs just changed."""
    if not written_keys:
        return
    with _TEST_CACHE_LOCK:
        for service, keys in _SERVICE_TESTER_KEYS.items():
            if keys & written_keys:
                _TEST_CACHE.pop(service, None)


@router.post("/api/settings/test/{service}")
def api_test_service(
    service: str, request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    """Test connectivity for a named service using current stored settings.

    Constraints layered on top of the dispatch:

    * Per-admin rate limit (10/min, 60/day) — without it a logged-in
      attacker could chain test calls to flood Plex / Mailgun.
    * Decryption is restricted to the keys this tester actually needs
      (see :data:`_SERVICE_TESTER_KEYS`). The previous code decrypted
      every secret in the DB on every test, which is a needless plain-
      text exposure window.
    * Each tester runs under a hard 15 s wall-clock cap. An unreachable
      Plex used to pin the request thread for 35 s through stacked
      timeouts; that's a self-inflicted DoS vector.
    """
    tester = _SERVICE_TESTERS.get(service)
    if tester is None:
        return JSONResponse({"ok": False, "error": f"Unknown service: {service}"}, status_code=400)

    cached = _cache_get(service)
    if cached is not None:
        return JSONResponse(cached)

    if not _SETTINGS_TEST_LIMITER.check(admin):
        logger.warning("settings.test_throttled user=%s service=%s", admin, service)
        return JSONResponse(
            {"ok": False, "error": "Too many service tests — slow down"},
            status_code=429,
        )

    conn = get_db()
    config = request.app.state.config
    needed_keys = _SERVICE_TESTER_KEYS.get(service)
    try:
        settings = _load_settings(conn, config.secret_key, keys=needed_keys)
    except ConfigDecryptError as exc:
        # The decryption failure is real — the operator probably rotated
        # MEDIAMAN_SECRET_KEY without rotating the stored ciphertexts.
        # Surface that distinctly from "no key configured" / "auth
        # failed at the remote service".
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
                # The worker thread is still running — we can't kill it,
                # but we MUST return promptly. The thread will finish on
                # its own and its result will be discarded.
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
