"""``SafeHTTPClient`` — SSRF- and size-aware outbound HTTP wrapper.

Responsibility
--------------
Compose :mod:`.dns_pinning`, :mod:`.streaming`, and :mod:`.retry` into a
single class that every outbound service call (Radarr, Sonarr, Plex, TMDB,
OMDb, Mailgun, NZBGet, OpenAI) passes through.

The six safety properties it enforces, in order:

1. **SSRF re-validation** — :func:`~mediaman.services.infra.url_safety.resolve_safe_outbound_url`
   is called on every request, re-resolving DNS so a one-off whitelist check
   cannot be bypassed by a DNS rebind attack.
2. **DNS pinning** — the IP returned by the SSRF guard is pinned via
   :func:`~.dns_pinning.pin` for the duration of the request, closing the
   window between the safety check and the actual ``socket.getaddrinfo`` call.
3. **No redirects** — ``allow_redirects=False`` ensures the final response
   comes from the host we validated, not from a redirect target.
4. **Split timeout** — ``(connect=5, read=30)`` so a slow-loris read cannot
   pin a worker for the full minute a single-value timeout would allow.
5. **Size cap** — response bodies are streamed and aborted at ``max_bytes``
   (default 8 MiB), preventing a compromised upstream from exhausting memory.
6. **Retry only on idempotent methods** — GET retries 429/5xx by default;
   POST/PUT/DELETE never retry unless the caller passes ``retry=True``.

Errors are raised as :class:`SafeHTTPError`, which carries the final status
code, a truncated body snippet, and the URL so callers can log or surface the
failure without digging into a ``requests.Response``.

Patchability note
-----------------
``_dispatch`` and ``resolve_safe_outbound_url`` are resolved at call time
through the ``mediaman.services.infra.http_client`` module namespace (via
``sys.modules``) rather than via a static import binding.  This is required
so that ``monkeypatch.setattr(http_client, "_dispatch", ...)`` in tests
intercepts the actual transport call — otherwise pytest's monkeypatch would
change the name in the wrong module dict and the original function would still
be invoked.  The same applies to ``resolve_safe_outbound_url``.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import sys
import time  # noqa: F401 — tests patch http.client.time.sleep via monkeypatch
from typing import Any

import requests

from mediaman.core.url_safety import resolve_safe_outbound_url as _resolve_safe_outbound_url
from mediaman.services.infra.http.dns_pinning import ensure_hook_installed, pin
from mediaman.services.infra.http.retry import _RETRY_BACKOFFS, dispatch_loop
from mediaman.services.infra.http.streaming import _read_capped

logger = logging.getLogger("mediaman")

# Public alias so tests can monkeypatch ``http.client.resolve_safe_outbound_url``
# and have ``_request`` pick up the patched version via sys.modules lookup.
resolve_safe_outbound_url = _resolve_safe_outbound_url

_HTTP_CLIENT_MODULE = "mediaman.services.infra.http.client"

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Default per-call timeouts.  Connect is short — a TCP handshake that hasn't
#: completed in 5 s means the target is unreachable.  Read is generous because
#: OpenAI and TMDB occasionally take 20-30 s.
_DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 30.0)

#: Default response-size cap.  8 MiB is well above any sane JSON API payload
#: but low enough that a pathological upstream cannot pin memory on a worker.
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def _build_user_agent() -> str:
    """Return ``mediaman/<version>`` for outbound HTTP attribution.

    Lazy import keeps this module free of an early package-level import.
    Falls back to a fixed string when the version cannot be resolved (e.g.
    running uninstalled from source).
    """
    try:
        from mediaman import __version__ as version

        return f"mediaman/{version}"
    except Exception:
        return "mediaman/dev"


_USER_AGENT = _build_user_agent()


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class SafeHTTPError(Exception):
    """Raised when a :class:`SafeHTTPClient` call returns a non-2xx.

    Carries the final status code, a truncated body snippet, and the URL that
    failed.  The snippet is UTF-8 best-effort — binary bodies are replaced
    rather than raising a secondary decode error.
    """

    def __init__(self, status_code: int, body_snippet: str, url: str) -> None:
        self.status_code = status_code
        self.body_snippet = body_snippet
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}: {body_snippet[:120]}")

    def json_error(self) -> dict | None:
        """Return the parsed JSON error body, or ``None`` if not JSON."""
        try:
            parsed = _json.loads(self.body_snippet)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Low-level transport function
# ---------------------------------------------------------------------------


def _dispatch(
    caller: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None,
    params: dict[str, Any] | None,
    json: Any,
    data: Any,
    auth: Any,
    timeout: tuple[float, float],
) -> requests.Response:
    """Issue a single HTTP request via *caller* with safe defaults.

    Split out so tests can patch the transport at one well-known point
    without caring about the retry / SSRF machinery above it.
    """
    return caller.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json,
        data=data,
        auth=auth,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class SafeHTTPClient:
    """SSRF- and size-aware wrapper around :mod:`requests`.

    Instantiate one per logical outbound target so the connection pool
    (``session``) can be reused.  Every call re-validates the target URL via
    :func:`~mediaman.services.infra.url_safety.resolve_safe_outbound_url`,
    which re-resolves DNS — this is the DNS-rebind defence.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        session: requests.Session | None = None,
        default_timeout: tuple[float, float] = _DEFAULT_TIMEOUT,
        default_max_bytes: int = _DEFAULT_MAX_BYTES,
        strict_egress: bool | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else ""
        # If the caller didn't supply a Session, create a private one so the
        # connection pool is reused across calls.
        self._session = session or requests.Session()
        # Stamp every outbound request with our own User-Agent so server
        # operators can identify mediaman in their logs.  The previous default
        # ``python-requests/x.y.z`` gets aggressive rate-limit treatment on a
        # few upstreams (TMDB, OMDb).
        existing_ua = self._session.headers.get("User-Agent")
        if existing_ua is None or str(existing_ua).startswith("python-requests/"):
            self._session.headers["User-Agent"] = _USER_AGENT
        self._default_timeout = default_timeout
        self._default_max_bytes = default_max_bytes
        self._strict_egress = strict_egress

    # ------------------------------------------------------------------
    # Public verb methods
    # ------------------------------------------------------------------

    def get(
        self,
        path_or_url: str,
        *,
        retry: bool = True,
        timeout: tuple[float, float] | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        auth: Any = None,
        expected_content_type: str | None = None,
    ) -> requests.Response:
        """Perform an SSRF-checked GET; retries 429/5xx by default."""
        return self._request(
            "GET",
            path_or_url,
            retry=retry,
            timeout=timeout,
            max_bytes=max_bytes,
            headers=headers,
            params=params,
            json=json,
            data=data,
            auth=auth,
            expected_content_type=expected_content_type,
        )

    def post(
        self,
        path_or_url: str,
        *,
        retry: bool = False,
        timeout: tuple[float, float] | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        auth: Any = None,
        expected_content_type: str | None = None,
    ) -> requests.Response:
        """Perform an SSRF-checked POST; no retries unless ``retry=True``."""
        return self._request(
            "POST",
            path_or_url,
            retry=retry,
            timeout=timeout,
            max_bytes=max_bytes,
            headers=headers,
            params=params,
            json=json,
            data=data,
            auth=auth,
            expected_content_type=expected_content_type,
        )

    def put(
        self,
        path_or_url: str,
        *,
        retry: bool = False,
        timeout: tuple[float, float] | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        auth: Any = None,
        expected_content_type: str | None = None,
    ) -> requests.Response:
        """Perform an SSRF-checked PUT; no retries unless ``retry=True``."""
        return self._request(
            "PUT",
            path_or_url,
            retry=retry,
            timeout=timeout,
            max_bytes=max_bytes,
            headers=headers,
            params=params,
            json=json,
            data=data,
            auth=auth,
            expected_content_type=expected_content_type,
        )

    def delete(
        self,
        path_or_url: str,
        *,
        retry: bool = False,
        timeout: tuple[float, float] | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        auth: Any = None,
        expected_content_type: str | None = None,
    ) -> requests.Response:
        """Perform an SSRF-checked DELETE; no retries unless ``retry=True``."""
        return self._request(
            "DELETE",
            path_or_url,
            retry=retry,
            timeout=timeout,
            max_bytes=max_bytes,
            headers=headers,
            params=params,
            json=json,
            data=data,
            auth=auth,
            expected_content_type=expected_content_type,
        )

    # ------------------------------------------------------------------
    # Internal machinery
    # ------------------------------------------------------------------

    def _resolve_url(self, path_or_url: str) -> str:
        """Resolve *path_or_url* against ``base_url`` if it isn't absolute."""
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not self._base_url:
            return path_or_url
        if path_or_url.startswith("/"):
            return f"{self._base_url}{path_or_url}"
        return f"{self._base_url}/{path_or_url}"

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        retry: bool = False,
        timeout: tuple[float, float] | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        auth: Any = None,
        expected_content_type: str | None = None,
    ) -> requests.Response:
        """Dispatch a single HTTP call with all the safety machinery.

        ``_dispatch`` and ``resolve_safe_outbound_url`` are resolved at call
        time from the ``http_client`` module namespace (via ``sys.modules``)
        so that ``monkeypatch.setattr(http_client, "_dispatch", ...)`` in
        tests correctly intercepts the transport call.  See the module
        docstring for the rationale.

        Args:
            method: HTTP verb (``"GET"``, ``"POST"``, …).
            path_or_url: Absolute URL or a path resolved against ``base_url``.
            retry: When ``True``, retry transient transport errors and 429/5xx
                responses.  Honoured automatically for GETs.
            timeout: ``(connect, read)`` tuple, defaulting to ``(5.0, 30.0)``.
            max_bytes: Hard cap on the response body.
            headers, params, json, data, auth: Forwarded to the underlying
                ``requests`` call.
            expected_content_type: When set, the response's ``Content-Type``
                header must start with this value (case-insensitive,
                parameter-stripped).  A mismatch raises :class:`SafeHTTPError`.
        """
        ensure_hook_installed()
        url = self._resolve_url(path_or_url)

        # Resolve ``resolve_safe_outbound_url`` through ``http_client``'s module
        # namespace at call time so tests can patch it via
        # ``monkeypatch.setattr(http_client, "resolve_safe_outbound_url", ...)``.
        _http_client_mod = sys.modules.get(_HTTP_CLIENT_MODULE)
        _resolve = (
            getattr(_http_client_mod, "resolve_safe_outbound_url", None)
            if _http_client_mod is not None
            else None
        ) or _resolve_safe_outbound_url

        safe, hostname, pinned_ip = _resolve(url, strict_egress=self._strict_egress)
        if not safe:
            raise SafeHTTPError(
                status_code=0,
                body_snippet="refused by SSRF guard",
                url=url,
            )

        timeout = timeout or self._default_timeout
        cap = max_bytes if max_bytes is not None else self._default_max_bytes
        caller = self._session
        attempts = 1 + (len(_RETRY_BACKOFFS) if retry else 0)

        def _dispatch_fn() -> requests.Response:
            # Resolve ``_dispatch`` through ``http_client``'s module namespace
            # at call time so tests can patch it with monkeypatch.
            _hc = sys.modules.get(_HTTP_CLIENT_MODULE)
            _d = getattr(_hc, "_dispatch", None) if _hc is not None else None
            if _d is None:
                _d = _dispatch
            return _d(
                caller,
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                data=data,
                auth=auth,
                timeout=timeout,
            )

        def _read_fn(response: requests.Response) -> bytes:
            return _read_capped(
                response,
                cap,
                expected_content_type=expected_content_type,
            )

        ctx = pin(hostname, pinned_ip) if (hostname and pinned_ip) else contextlib.nullcontext()
        with ctx:
            return dispatch_loop(
                dispatch_fn=_dispatch_fn,
                read_fn=_read_fn,
                method=method,
                url=url,
                attempts=attempts,
                make_error=SafeHTTPError,
            )
