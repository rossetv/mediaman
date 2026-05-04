"""Settings service-connection testers.

Owns:
- The ``@register`` decorator and the ``TESTERS`` registry dict that
  maps a service name to its test callable.
- The ``SERVICE_TESTER_KEYS`` allow-list, which restricts ``_load_settings``
  to the minimal set of DB keys each tester actually needs.
- The in-memory result cache (``TEST_CACHE``) and its invalidation helper.
- One tester function per service (``test_plex``, ``test_sonarr``, …).
- Shared internal helpers used by multiple testers
  (``_safe_http_error_to_response``, ``_test_bearer_api``).

No FastAPI routes live here — callers import TESTERS and invoke the
registered functions directly inside the HTTP endpoint defined in the
package ``__init__``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from fastapi.responses import JSONResponse

from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError
from mediaman.web.models import _API_KEY_RE

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

#: In-memory cache of the most recent tester payload per service.
#: Shared across admins because the underlying settings are global.
#: Invalidated on any settings write that touches the service's keys.
TEST_CACHE: dict[str, tuple[float, dict]] = {}
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


def _register(name: str) -> Callable[[Callable], Callable]:
    """Register a tester function in :data:`TESTERS` under *name*."""

    def decorator(fn: Callable) -> Callable:
        TESTERS[name] = fn
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Shared internal helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Per-service testers
# ---------------------------------------------------------------------------


@_register("plex")
def test_plex(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to Plex using the configured URL and token."""
    from mediaman.services.media_meta.plex import PlexClient

    url = str(settings.get("plex_url") or "")
    token = str(settings.get("plex_token") or "")
    if not url or not token:
        return JSONResponse({"ok": False, "error": "Plex URL and token are required"})
    PlexClient(url, token).get_libraries()
    return JSONResponse({"ok": True})


@_register("sonarr")
def test_sonarr(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to Sonarr using the configured URL and API key."""
    from mediaman.services.arr.sonarr import SonarrClient

    url = str(settings.get("sonarr_url") or "")
    api_key = str(settings.get("sonarr_api_key") or "")
    if not url or not api_key:
        return JSONResponse({"ok": False, "error": "Sonarr URL and API key are required"})
    ok = SonarrClient(url, api_key).test_connection()
    return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})


@_register("radarr")
def test_radarr(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to Radarr using the configured URL and API key."""
    from mediaman.services.arr.radarr import RadarrClient

    url = str(settings.get("radarr_url") or "")
    api_key = str(settings.get("radarr_api_key") or "")
    if not url or not api_key:
        return JSONResponse({"ok": False, "error": "Radarr URL and API key are required"})
    ok = RadarrClient(url, api_key).test_connection()
    return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})


@_register("nzbget")
def test_nzbget(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to NZBGet using the configured URL and credentials."""
    from mediaman.services.downloads.nzbget import NzbgetClient

    url = str(settings.get("nzbget_url") or "")
    username = str(settings.get("nzbget_username") or "")
    password = str(settings.get("nzbget_password") or "")
    if not url:
        return JSONResponse({"ok": False, "error": "NZBGet URL is required"})
    ok = NzbgetClient(url, username, password).test_connection()
    return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})


@_register("mailgun")
def test_mailgun(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to Mailgun using the configured domain and API key."""
    from mediaman.services.mail.mailgun import MailgunClient

    domain = str(settings.get("mailgun_domain") or "")
    api_key = str(settings.get("mailgun_api_key") or "")
    from_address = str(settings.get("mailgun_from_address") or "")
    if not domain or not api_key:
        return JSONResponse({"ok": False, "error": "Mailgun domain and API key are required"})
    ok = MailgunClient(domain, api_key, from_address).test_connection()
    return JSONResponse({"ok": ok} if ok else {"ok": False, "error": "Connection failed"})


@_register("openai")
def test_openai(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to OpenAI using the configured API key."""
    api_key = str(settings.get("openai_api_key") or "")
    if not api_key:
        return JSONResponse({"ok": False, "error": "OpenAI API key is required"})
    if not _API_KEY_RE.match(api_key):
        return JSONResponse(
            {"ok": False, "error": "auth_failed: API key contains invalid characters"}
        )
    return _test_bearer_api(_OPENAI_MODELS_URL, api_key)


@_register("tmdb")
def test_tmdb(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to TMDB using the configured read token."""
    read_token = str(settings.get("tmdb_read_token") or "")
    if not read_token:
        return JSONResponse({"ok": False, "error": "TMDB Read Token is required"})
    if not _API_KEY_RE.match(read_token):
        return JSONResponse(
            {"ok": False, "error": "auth_failed: token contains invalid characters"}
        )
    return _test_bearer_api(_TMDB_CONFIG_URL, read_token)


@_register("omdb")
def test_omdb(settings: dict[str, object]) -> JSONResponse:
    """Test connectivity to OMDb using the configured API key."""
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


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def cache_get(service: str) -> dict | None:
    """Return the cached test result for *service*, or ``None`` if absent/stale."""
    with _TEST_CACHE_LOCK:
        entry = TEST_CACHE.get(service)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            TEST_CACHE.pop(service, None)
            return None
        return payload


def cache_put(service: str, payload: dict) -> None:
    """Store *payload* as the cached result for *service*."""
    with _TEST_CACHE_LOCK:
        TEST_CACHE[service] = (time.monotonic() + TEST_CACHE_TTL_SECONDS, payload)


def invalidate_test_cache_for_keys(written_keys: set[str]) -> None:
    """Drop cached test results for any service whose inputs just changed."""
    if not written_keys:
        return
    with _TEST_CACHE_LOCK:
        for service, keys in SERVICE_TESTER_KEYS.items():
            if keys & written_keys:
                TEST_CACHE.pop(service, None)
