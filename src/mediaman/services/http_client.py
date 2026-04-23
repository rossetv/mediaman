"""Central, SSRF-aware HTTP client for outbound service calls.

Every admin-configured outbound request (Radarr, Sonarr, Plex posters,
TMDB, OMDb, Mailgun, NZBGet, OpenAI) routes through :class:`SafeHTTPClient`.
It exists because stock ``requests.get(url, timeout=15)`` on a
user-controlled URL gives an attacker who lands an admin session a broad
SSRF primitive: a 302 to ``169.254.169.254`` leaks API keys into cloud
metadata, an oversize body fills memory, and DNS-rebind swaps the
resolved IP between the pre-request safety check and the actual
connect.

The class does five things:

* Re-runs :func:`mediaman.services.url_safety.is_safe_outbound_url` on
  every call so DNS-rebind cannot slip past a one-off validation.
* Forces ``allow_redirects=False`` — responses must be final at the
  target host we just validated.
* Splits the single 15-second timeout into ``(connect=5, read=30)`` so
  a slow-lorris read cannot pin a worker for a full minute.
* Streams response bodies and aborts at ``max_bytes`` (default 8 MiB),
  so a compromised upstream cannot pin memory.
* Retries only safe, idempotent GETs on 429/502/503/504 — POST/PUT/DELETE
  never retry unless the caller opts in via ``retry=True``.

Errors are raised as :class:`SafeHTTPError`, which carries the final
status code, a truncated body snippet, and the URL so callers can log
or surface the failure without digging into a requests ``Response``.
"""

from __future__ import annotations

import json as _json
import logging
import time
from typing import Any

import requests

from mediaman.services.url_safety import is_safe_outbound_url

logger = logging.getLogger("mediaman")

#: Default per-call timeouts. Connect is short — a TCP handshake that
#: hasn't completed in 5 s means the target is unreachable. Read is
#: generous because OpenAI and TMDB occasionally take 20-30 s.
_DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 30.0)

#: Default response-size cap. 8 MiB is well above any sane JSON API
#: payload but low enough that a pathological upstream cannot pin
#: memory on a worker. Callers that know their payloads are smaller
#: (posters, status endpoints) pass a tighter cap.
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024

#: HTTP status codes that we treat as transient on idempotent requests.
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

#: Backoff schedule for retries — two extra attempts after the first,
#: so a worst case is three total requests.
_RETRY_BACKOFFS: tuple[float, ...] = (0.5, 1.0)

#: Bytes of the response body to include in a :class:`SafeHTTPError` for
#: log / debugging purposes. Kept small so accidental logging of an
#: error snippet never dumps a full HTML error page.
_BODY_SNIPPET_BYTES = 512


class SafeHTTPError(Exception):
    """Raised when a :class:`SafeHTTPClient` call returns a non-2xx.

    Carries the final status code, a truncated body snippet, and the
    URL that failed. The snippet is UTF-8 best-effort — binary bodies
    are replaced rather than raising a secondary decode error.
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


class SafeHTTPClient:
    """SSRF- and size-aware wrapper around :mod:`requests`.

    Instantiate one per logical outbound target so the connection pool
    (``session``) can be reused. Every call re-validates the target URL
    via :func:`is_safe_outbound_url`, which re-resolves DNS — this is
    the DNS-rebind defence.
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
        self._session = session
        self._default_timeout = default_timeout
        self._default_max_bytes = default_max_bytes
        self._strict_egress = strict_egress

    # ------------------------------------------------------------------
    # Public verb methods
    # ------------------------------------------------------------------

    def get(self, path_or_url: str, **kwargs: Any) -> requests.Response:
        """Perform an SSRF-checked GET; retries 429/5xx on GETs by default."""
        kwargs.setdefault("retry", True)
        return self._request("GET", path_or_url, **kwargs)

    def post(self, path_or_url: str, **kwargs: Any) -> requests.Response:
        """Perform an SSRF-checked POST; no retries unless ``retry=True``."""
        kwargs.setdefault("retry", False)
        return self._request("POST", path_or_url, **kwargs)

    def put(self, path_or_url: str, **kwargs: Any) -> requests.Response:
        """Perform an SSRF-checked PUT; no retries unless ``retry=True``."""
        kwargs.setdefault("retry", False)
        return self._request("PUT", path_or_url, **kwargs)

    def delete(self, path_or_url: str, **kwargs: Any) -> requests.Response:
        """Perform an SSRF-checked DELETE; no retries unless ``retry=True``."""
        kwargs.setdefault("retry", False)
        return self._request("DELETE", path_or_url, **kwargs)

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
        stream: bool = False,
    ) -> requests.Response:
        """Dispatch a single HTTP call with all the safety machinery.

        The ``stream`` kwarg is accepted and passed through only for the
        benefit of callers that want to read the streamed body directly
        — the size cap is always enforced internally regardless.
        """
        url = self._resolve_url(path_or_url)
        if not is_safe_outbound_url(url, strict_egress=self._strict_egress):
            raise SafeHTTPError(
                status_code=0,
                body_snippet="refused by SSRF guard",
                url=url,
            )

        timeout = timeout or self._default_timeout
        cap = max_bytes if max_bytes is not None else self._default_max_bytes
        caller = self._session or requests

        attempts = 1 + (len(_RETRY_BACKOFFS) if retry else 0)
        last_exc: Exception | None = None
        last_status: int | None = None
        last_snippet: str = ""

        for attempt in range(attempts):
            try:
                response = _dispatch(
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
            except (requests.Timeout, requests.ConnectionError) as exc:
                logger.warning(
                    "HTTP %s %s transport error: %s",
                    method, _safe_path(url), type(exc).__name__,
                )
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(_RETRY_BACKOFFS[attempt])
                    continue
                raise SafeHTTPError(
                    status_code=0,
                    body_snippet=f"transport error: {type(exc).__name__}",
                    url=url,
                ) from exc

            try:
                body = _read_capped(response, cap)
            except _SizeCapExceeded as exc:
                response.close()
                raise SafeHTTPError(
                    status_code=response.status_code,
                    body_snippet=str(exc),
                    url=url,
                ) from None

            # Re-attach the buffered body on the response object so the
            # caller can use .json(), .text, .content as normal.
            response._content = body  # type: ignore[attr-defined]
            response._content_consumed = True  # type: ignore[attr-defined]

            if response.status_code in _RETRYABLE_STATUSES and attempt + 1 < attempts:
                logger.warning(
                    "HTTP %s %s returned %s — retrying",
                    method, _safe_path(url), response.status_code,
                )
                response.close()
                time.sleep(_RETRY_BACKOFFS[attempt])
                last_status = response.status_code
                last_snippet = _snippet(body)
                continue

            if not (200 <= response.status_code < 300):
                snippet = _snippet(body)
                response.close()
                raise SafeHTTPError(
                    status_code=response.status_code,
                    body_snippet=snippet,
                    url=url,
                )

            return response

        # All retries exhausted on a retryable status.
        if last_status is not None:
            raise SafeHTTPError(
                status_code=last_status,
                body_snippet=last_snippet,
                url=url,
            )
        # Should be unreachable; keep mypy/readers happy.
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def raise_for_status(response: requests.Response) -> None:
        """Raise :class:`SafeHTTPError` if *response* is non-2xx.

        Helper for call-sites that receive a raw response from somewhere
        else (e.g. plexapi) but still want mediaman's error shape.
        """
        if 200 <= response.status_code < 300:
            return
        body = response.content if getattr(response, "_content_consumed", False) else b""
        raise SafeHTTPError(
            status_code=response.status_code,
            body_snippet=_snippet(body),
            url=response.url or "",
        )


# ----------------------------------------------------------------------
# Module-private helpers
# ----------------------------------------------------------------------


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


class _SizeCapExceeded(Exception):
    """Internal signal that the streamed body breached ``max_bytes``."""


def _read_capped(response: requests.Response, max_bytes: int) -> bytes:
    """Read *response* body up to *max_bytes*, raising if the cap is hit.

    Honours an advertised Content-Length as a fast-fail before reading
    anything, then streams chunks to catch servers that omit or lie
    about Content-Length.
    """
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise _SizeCapExceeded(
                    f"response body too large: declared {declared} > cap {max_bytes}"
                )
        except ValueError:
            # Malformed header — fall through to streaming enforcement.
            pass
    buf = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise _SizeCapExceeded(
                f"response body exceeded cap of {max_bytes} bytes"
            )
    return bytes(buf)


def _snippet(body: bytes) -> str:
    """Return a short UTF-8 snippet of *body* suitable for error messages."""
    if not body:
        return ""
    return body[:_BODY_SNIPPET_BYTES].decode("utf-8", errors="replace")


def _safe_path(url: str) -> str:
    """Return the path of *url* for log messages, never the query string.

    We deliberately log path only — query strings on outbound URLs often
    contain API keys or tokens.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or "?"
        return f"{host}{parsed.path or '/'}"
    except Exception:
        return "?"
