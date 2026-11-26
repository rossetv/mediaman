"""Settings service-connection testers: the @register decorator and TESTERS registry, the
SERVICE_TESTER_KEYS allow-list that limits _load_settings to the keys each tester needs, the
TEST_CACHE result cache, one tester per service and shared helpers. No routes live here; the
endpoint in the package __init__ invokes the registered functions.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Protocol

import requests as _requests
from defusedxml.ElementTree import ParseError
from fastapi.responses import JSONResponse
from plexapi.exceptions import BadRequest, NotFound, Unauthorized

from mediaman.services.downloads.nzbget import NzbgetError
from mediaman.services.infra import SafeHTTPClient, SafeHTTPError
from mediaman.services.media_meta.omdb import clear_refusal
from mediaman.web.models import _API_KEY_RE
from mediaman.web.responses import respond_failure

logger = logging.getLogger(__name__)


class _ConnectionTestable(Protocol):
    def check_reachable(self) -> None: ...


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

#: How often each service's settings were written, so a test that began before a write is not kept.
_TEST_GENERATIONS: dict[str, int] = {}

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


def _failure(code: str, message: str) -> JSONResponse:
    """A failed test: ``error`` is the machine code, ``message`` the sentence the page shows."""
    return JSONResponse({"ok": False, "error": code, "message": message})


_REJECTED = "The service rejected the credentials."
_TIMED_OUT = "The service didn't answer in time."
_UNREACHABLE = "Couldn't reach the service."
_BAD_REPLY = "The service sent a reply that wasn't recognised."
_BAD_KEY_CHARS = "The key contains characters that aren't allowed."


def _safe_http_error_to_response(exc: SafeHTTPError) -> JSONResponse:
    """Convert a SafeHTTPError to a test-result JSONResponse, covering SSRF refusal, transport
    errors (timeout, connection refused) and HTTP auth failures; other status codes get a
    generic message.
    """
    if exc.status_code == 0:
        snippet = exc.body_snippet
        if "refused by SSRF" in snippet:
            return _failure("ssrf_refused", "That address isn't allowed.")
        if "transport error" in snippet:
            if "timeout" in snippet.lower():
                return _failure("timeout", _TIMED_OUT)
            return _failure("connection_refused", _UNREACHABLE)
    if exc.status_code in (401, 403):
        return _failure("auth_failed", _REJECTED)
    return _failure(
        f"other: HTTP {exc.status_code}",
        f"The service answered with an error (HTTP {exc.status_code}).",
    )


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
            return respond_failure(required_msg, status=200)
        resolved[arg_name] = value
    for arg_name, setting_name in (optional_keys or {}).items():
        resolved[arg_name] = str(settings.get(setting_name) or "")
    try:
        make_client(resolved).check_reachable()
    except SafeHTTPError as exc:
        return _safe_http_error_to_response(exc)
    except _requests.exceptions.JSONDecodeError:
        # A 200 whose body isn't JSON (a proxy login page, a wrong port) is
        # a reply we can't read, not an unreachable service.
        return _failure("bad_reply", _BAD_REPLY)
    except _requests.RequestException as exc:
        if isinstance(exc, _requests.Timeout):
            return _failure("timeout", _TIMED_OUT)
        return _failure("connection_refused", _UNREACHABLE)
    except NzbgetError:
        return _failure("bad_reply", _BAD_REPLY)
    return JSONResponse({"ok": True})


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
        return respond_failure("Plex URL and token are required", status=200)
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
        if isinstance(exc, _requests.Timeout):
            return _failure("timeout", _TIMED_OUT)
        return _failure("connection_refused", _UNREACHABLE)
    except Unauthorized:
        logger.warning("settings.test_plex token refused by Plex")
        return _failure("auth_failed", "Plex refused the token.")
    except BadRequest as exc:
        # plexapi raises BadRequest for every status but 401 and 404 and carries the
        # status only in the message, as "(503) service_unavailable; ...".
        status = re.match(r"\((\d{3})\)", str(exc))
        if status and int(status.group(1)) >= 500:
            logger.warning("settings.test_plex Plex answered with an error: status %s", status[1])
            return _failure("plex_error", "Plex answered with an error. Try again in a moment.")
        return _not_plex(exc)
    except NotFound as exc:
        return _not_plex(exc)
    except (ParseError, TypeError, AttributeError) as exc:
        if not is_not_plex_reply(exc):
            raise
        return _not_plex(exc)


def is_not_plex_reply(exc: Exception) -> bool:
    """Whether ``exc`` is what plexapi raises on a 200 that is not a Plex server's answer.

    A web page or another service's JSON does not parse as XML; an empty body, or XML
    that is not Plex's, parses to nothing and plexapi then fails on that ``None``. Any
    other TypeError or AttributeError is a programming error and is not matched.
    """
    if isinstance(exc, ParseError):
        return True
    return isinstance(exc, (TypeError, AttributeError)) and "'NoneType'" in str(exc)


def _not_plex(exc: Exception) -> JSONResponse:
    logger.warning("settings.test_plex not a Plex server: %s", type(exc).__name__)
    return _failure("not_plex", "No Plex server answered at this address.")


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
        return respond_failure("OpenAI API key is required", status=200)
    if not _API_KEY_RE.match(api_key):
        return _failure("auth_failed: API key contains invalid characters", _BAD_KEY_CHARS)
    return _test_bearer_api(_OPENAI_MODELS_URL, api_key)


@_register("tmdb")
def test_tmdb(settings: dict[str, object]) -> JSONResponse:
    read_token = str(settings.get("tmdb_read_token") or "")
    if not read_token:
        return respond_failure("TMDB Read Token is required", status=200)
    if not _API_KEY_RE.match(read_token):
        return _failure("auth_failed: token contains invalid characters", _BAD_KEY_CHARS)
    return _test_bearer_api(_TMDB_CONFIG_URL, read_token)


@_register("omdb")
def test_omdb(settings: dict[str, object]) -> JSONResponse:
    api_key = str(settings.get("omdb_api_key") or "")
    if not api_key:
        return respond_failure("OMDB API key is required", status=200)
    if not _API_KEY_RE.match(api_key):
        return _failure("auth_failed: API key contains invalid characters", _BAD_KEY_CHARS)
    try:
        # Tester hits a pinned host (www.omdbapi.com); no conn-derived allowlist applies.
        resp = SafeHTTPClient().get(
            _OMDB_TEST_URL,
            params={"apikey": api_key, "i": "tt0111161"},
            timeout=(5.0, 15.0),
        )
        data = resp.json()
        if data.get("Response") == "True":
            clear_refusal(api_key)
            return JSONResponse({"ok": True})
        omdb_error = data.get("Error")
        if omdb_error:
            return JSONResponse({"ok": False, "error": omdb_error, "message": omdb_error})
        return _failure("auth_failed", _REJECTED)
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


def cache_generation(service: str) -> int:
    """Return what `cache_put` takes; read it before the settings the test is of."""
    with _TEST_CACHE_LOCK:
        return _TEST_GENERATIONS.get(service, 0)


def cache_put(
    service: str, payload: dict[str, object], status_code: int = 200, *, generation: int
) -> None:
    """Cache a successful result with the status it was sent with. A failure is never
    cached, so the next press of Test checks the service again; nor is a result whose
    settings have been written since *generation*."""
    if payload.get("ok") is not True:
        return
    with _TEST_CACHE_LOCK:
        if generation != _TEST_GENERATIONS.get(service, 0):
            return
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
                _TEST_GENERATIONS[service] = _TEST_GENERATIONS.get(service, 0) + 1
