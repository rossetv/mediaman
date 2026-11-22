"""Settings service-connection testers: the @register decorator and TESTERS registry, the
SERVICE_TESTER_KEYS allow-list that limits _load_settings to the keys each tester needs, the
TEST_CACHE result cache, one tester per service and shared helpers. No routes live here; the
endpoint in the package __init__ invokes the registered functions.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

import requests as _requests
from fastapi.responses import JSONResponse

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError
from mediaman.web.models import _API_KEY_RE
from mediaman.web.responses import respond_failure


class _ConnectionTestable(Protocol):
    def is_reachable(self) -> bool: ...


#: OpenAI models endpoint used by the connectivity test.
_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"

#: TMDB configuration endpoint used by the connectivity test.
_TMDB_CONFIG_URL = "https://api.themoviedb.org/3/configuration"

#: OMDb root endpoint used by the connectivity test.
_OMDB_TEST_URL = "https://www.omdbapi.com/"

#: Per-tester upper bound on time spent in a single tester() call.  An
#: unreachable Plex / Sonarr / mailgun must not pin the request thread
#: indefinitely — without this cap, a 35 s underlying timeout was
#: observable when chained with retries and reverse-proxy buffering.
TESTER_TIMEOUT_SECONDS = 15.0

#: TTL for the service-test result cache.  The settings page auto-fires
#: a test for every configured service on load, which trivially blows
#: the per-admin rate limit (10/min) on a couple of reloads.  Caching
#: the result for 120 s makes reloads cheap without hiding genuine
#: connectivity changes for long.
TEST_CACHE_TTL_SECONDS = 120.0

# Per-process tester result cache; shared across requests in the same worker
# to avoid repeat connection-test calls on settings page reloads.
#: In-memory cache of the most recent tester payload per service.
#: Shared across admins because the underlying settings are global.
#: Invalidated on any settings write that touches the service's keys.
TEST_CACHE: dict[str, tuple[float, dict[str, object], int]] = {}
_TEST_CACHE_LOCK = threading.Lock()

#: Registry mapping service name → test callable.
TESTERS: dict[str, Callable[[dict[str, object]], JSONResponse]] = {}

#: Per-tester key allow-list.  Each tester only needs a small subset of
#: settings — restricting ``_load_settings`` to that subset means
#: triggering one tester does NOT decrypt every other secret in the DB.
SERVICE_TESTER_KEYS: dict[str, set[str]] = {
    "plex": {"plex_url", "plex_token"},
    "sonarr": {"sonarr_url", "sonarr_api_key"},
    "radarr": {"radarr_url", "radarr_api_key"},
    "nzbget": {"nzbget_url", "nzbget_username", "nzbget_password"},
    "mailgun": {"mailgun_domain", "mailgun_api_key", "mailgun_from_address"},
    "openai": {"openai_api_key"},
    "tmdb": {"tmdb_read_token"},
    "omdb": {"omdb_api_key"},
}


_TesterFn = Callable[[dict[str, object]], JSONResponse]


def _register(name: str) -> Callable[[_TesterFn], _TesterFn]:

    def decorator(fn: _TesterFn) -> _TesterFn:
        TESTERS[name] = fn
        return fn

    return decorator


def _safe_http_error_to_response(exc: SafeHTTPError) -> JSONResponse:
    """Convert a SafeHTTPError to a test-result JSONResponse, covering SSRF refusal, transport
    errors (timeout, connection refused) and HTTP auth failures; other status codes get a
    generic message.
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
    """Test a Bearer-token-authenticated API endpoint, shared by the OpenAI and TMDB tests.
    Returns a JSONResponse and never raises.
    """
    try:
        # Tester validates an admin-typed bearer URL pre-save; no conn-derived allowlist applies.
        SafeHTTPClient().get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(5.0, 15.0),
        )
        return JSONResponse({"ok": True})
    except SafeHTTPError as exc:
        return _safe_http_error_to_response(exc)


def _test_connection_service(
    settings: dict[str, object],
    *,
    required_keys: dict[str, str],
    required_msg: str,
    optional_keys: dict[str, str] | None = None,
    make_client: Callable[[dict[str, str]], _ConnectionTestable],
) -> JSONResponse:
    resolved: dict[str, str] = {}
    for arg_name, setting_name in required_keys.items():
        value = str(settings.get(setting_name) or "")
        if not value:
            return respond_failure(required_msg)
        resolved[arg_name] = value
    for arg_name, setting_name in (optional_keys or {}).items():
        resolved[arg_name] = str(settings.get(setting_name) or "")
    ok = bool(make_client(resolved).is_reachable())
    return JSONResponse({"ok": True}) if ok else respond_failure("Connection failed")


@_register("plex")
def test_plex(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to Plex. PlexClient uses plexapi, which can raise a bare
    requests.RequestException as well as SafeHTTPError from the SafeHTTPClient wrapper; both
    are caught here.
    """
    from mediaman.services.media_meta.plex import PlexClient

    url = str(settings.get("plex_url") or "")
    token = str(settings.get("plex_token") or "")
    if not url or not token:
        return respond_failure("Plex URL and token are required")
    try:
        PlexClient(url, token).get_libraries()
        return JSONResponse({"ok": True})
    except SafeHTTPError as exc:
        return _safe_http_error_to_response(exc)
    except _requests.RequestException as exc:
        # rationale: PlexClient uses plexapi which issues its own requests
        # session (not always routed through SafeHTTPClient), so bare
        # RequestException can escape the SafeHTTPError wrapper.
        # Use isinstance rather than string-matching: requests.Timeout messages
        # say "timed out" not "timeout", so the string check silently mis-maps
        # Timeout → connection_refused.
        kind = "timeout" if isinstance(exc, _requests.Timeout) else "connection_refused"
        return JSONResponse({"ok": False, "error": kind})


@_register("sonarr")
def test_sonarr(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.arr.base import ArrClient
    from mediaman.services.arr.spec import SONARR_SPEC

    return _test_connection_service(
        settings,
        required_keys={"url": "sonarr_url", "api_key": "sonarr_api_key"},
        required_msg="Sonarr URL and API key are required",
        make_client=lambda v: ArrClient(SONARR_SPEC, v["url"], v["api_key"]),
    )


@_register("radarr")
def test_radarr(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.arr.base import ArrClient
    from mediaman.services.arr.spec import RADARR_SPEC

    return _test_connection_service(
        settings,
        required_keys={"url": "radarr_url", "api_key": "radarr_api_key"},
        required_msg="Radarr URL and API key are required",
        make_client=lambda v: ArrClient(RADARR_SPEC, v["url"], v["api_key"]),
    )


@_register("nzbget")
def test_nzbget(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.downloads.nzbget import NzbgetClient

    return _test_connection_service(
        settings,
        required_keys={"url": "nzbget_url"},
        required_msg="NZBGet URL is required",
        optional_keys={"username": "nzbget_username", "password": "nzbget_password"},
        make_client=lambda v: NzbgetClient(v["url"], v["username"], v["password"]),
    )


@_register("mailgun")
def test_mailgun(settings: dict[str, object]) -> JSONResponse:
    from mediaman.services.mail.mailgun import MailgunClient

    return _test_connection_service(
        settings,
        required_keys={"domain": "mailgun_domain", "api_key": "mailgun_api_key"},
        required_msg="Mailgun domain and API key are required",
        optional_keys={"from_address": "mailgun_from_address"},
        make_client=lambda v: MailgunClient(v["domain"], v["api_key"], v["from_address"]),
    )


@_register("openai")
def test_openai(settings: dict[str, object]) -> JSONResponse:
    api_key = str(settings.get("openai_api_key") or "")
    if not api_key:
        return respond_failure("OpenAI API key is required")
    if not _API_KEY_RE.match(api_key):
        return JSONResponse(
            {"ok": False, "error": "auth_failed: API key contains invalid characters"}
        )
    return _test_bearer_api(_OPENAI_MODELS_URL, api_key)


@_register("tmdb")
def test_tmdb(settings: dict[str, object]) -> JSONResponse:
    read_token = str(settings.get("tmdb_read_token") or "")
    if not read_token:
        return respond_failure("TMDB Read Token is required")
    if not _API_KEY_RE.match(read_token):
        return JSONResponse(
            {"ok": False, "error": "auth_failed: token contains invalid characters"}
        )
    return _test_bearer_api(_TMDB_CONFIG_URL, read_token)


@_register("omdb")
def test_omdb(settings: dict[str, object]) -> JSONResponse:
    api_key = str(settings.get("omdb_api_key") or "")
    if not api_key:
        return respond_failure("OMDB API key is required")
    if not _API_KEY_RE.match(api_key):
        return JSONResponse(
            {"ok": False, "error": "auth_failed: API key contains invalid characters"}
        )
    try:
        # Tester hits a pinned host (www.omdbapi.com); no conn-derived allowlist applies.
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


def cache_get(service: str) -> tuple[dict[str, object], int] | None:
    with _TEST_CACHE_LOCK:
        entry = TEST_CACHE.get(service)
        if entry is None:
            return None
        expires_at, payload, status_code = entry
        if time.monotonic() >= expires_at:
            TEST_CACHE.pop(service, None)
            return None
        return payload, status_code


def cache_put(service: str, payload: dict[str, object], status_code: int = 200) -> None:
    """Cache a successful result with the status it was sent with. A failure is never
    cached, so the next press of Test checks the service again."""
    if payload.get("ok") is not True:
        return
    with _TEST_CACHE_LOCK:
        TEST_CACHE[service] = (
            time.monotonic() + TEST_CACHE_TTL_SECONDS,
            payload,
            status_code,
        )


def invalidate_test_cache_for_keys(written_keys: set[str]) -> None:
    if not written_keys:
        return
    with _TEST_CACHE_LOCK:
        for service, keys in SERVICE_TESTER_KEYS.items():
            if keys & written_keys:
                TEST_CACHE.pop(service, None)
