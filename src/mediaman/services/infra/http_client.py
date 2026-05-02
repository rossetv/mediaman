"""Central, SSRF-aware HTTP client for outbound service calls.

Every admin-configured outbound request (Radarr, Sonarr, Plex posters,
TMDB, OMDb, Mailgun, NZBGet, OpenAI) routes through :class:`SafeHTTPClient`.
It exists because stock ``requests.get(url, timeout=15)`` on a
user-controlled URL gives an attacker who lands an admin session a broad
SSRF primitive: a 302 to ``169.254.169.254`` leaks API keys into cloud
metadata, an oversize body fills memory, and DNS-rebind swaps the
resolved IP between the pre-request safety check and the actual
connect.

The class does six things:

* Re-runs the SSRF guard
  (:func:`mediaman.services.infra.url_safety.resolve_safe_outbound_url`)
  on every call so DNS-rebind cannot slip past a one-off validation.
* **Pins** the validated IP for the duration of the request via
  :func:`pin_dns_for_request`, so the eventual ``socket.getaddrinfo``
  returns the same address we just verified — a rebinding hostname
  that returned a public IP for the safety check and an internal IP
  for the real connect is denied the rebind window.
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

import contextlib
import email.utils
import json as _json
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from mediaman.services.infra.url_safety import resolve_safe_outbound_url

logger = logging.getLogger("mediaman")

# ---------------------------------------------------------------------------
# DNS pinning
#
# urllib3 (and therefore ``requests``) calls ``socket.getaddrinfo`` from
# the connecting thread when it builds a new pool entry. We monkey-patch
# the symbol once at import time with a wrapper that consults a
# *thread-local* pin table; if a pin exists for the requested host we
# return that IP, otherwise we delegate to the real resolver. The pin
# is set by :func:`pin_dns_for_request` during ``SafeHTTPClient._request``
# and removed when the context exits, so unrelated callers see the
# unmodified ``getaddrinfo`` behaviour. Storage is :class:`threading.local`
# so concurrent worker threads (scan threads, FastAPI threadpool calls)
# never see each other's pins.
# ---------------------------------------------------------------------------

_DNS_PIN_LOCAL = threading.local()
_ORIG_GETADDRINFO = socket.getaddrinfo
_PIN_INSTALL_LOCK = threading.Lock()
_PIN_INSTALLED = False


def _patched_getaddrinfo(host, port, *args, **kwargs):  # pragma: no cover - thin wrapper
    """``socket.getaddrinfo`` wrapper that honours per-thread DNS pins.

    When a pin is set for *host* we synthesise a single ``getaddrinfo``
    record for the pinned IP, preserving the family that matches the
    address (v4 vs v6). If no pin is set, behaviour is identical to the
    upstream resolver.

    If the caller asked for a specific family (e.g. ``family=AF_INET6``)
    and the pin holds an address from the other family, an empty list
    is returned rather than a record with the "wrong" family — that's
    the same signal urllib3 uses to decide "no matching addresses, try
    the next strategy", and avoids handing the connection layer a
    record it cannot actually use.
    """
    pins: dict[str, str] | None = getattr(_DNS_PIN_LOCAL, "pins", None)
    if pins:
        ip = pins.get(host)
        if ip is not None:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            requested_family = kwargs.get("family")
            if requested_family is None and len(args) > 0:
                requested_family = args[0]
            # ``AF_UNSPEC`` (0) means "either family is fine" — preserve
            # that as a wildcard match. Any other explicit family that
            # disagrees with the pin's family must yield an empty list.
            if (
                requested_family
                and requested_family != socket.AF_UNSPEC
                and requested_family != family
            ):
                return []
            socktype = kwargs.get("type") or (args[1] if len(args) > 1 else 0)
            proto = kwargs.get("proto") or (args[2] if len(args) > 2 else 0)
            sockaddr = (ip, port or 0, 0, 0) if family == socket.AF_INET6 else (ip, port or 0)
            return [
                (
                    family,
                    socktype or socket.SOCK_STREAM,
                    proto,
                    "",
                    sockaddr,
                )
            ]
    return _ORIG_GETADDRINFO(host, port, *args, **kwargs)


def _install_dns_pin_hook() -> None:
    """Install :func:`_patched_getaddrinfo` in place of ``socket.getaddrinfo``.

    Idempotent and thread-safe. Called from the module import (so any
    request that goes through this client is automatically pinned-aware),
    and re-applied if anything in the process replaced the symbol after
    import — a defensive measure against test fixtures that reset
    ``socket.getaddrinfo`` between runs.
    """
    global _PIN_INSTALLED
    with _PIN_INSTALL_LOCK:
        if socket.getaddrinfo is _patched_getaddrinfo:
            _PIN_INSTALLED = True
            return
        socket.getaddrinfo = _patched_getaddrinfo
        _PIN_INSTALLED = True


def _ensure_dns_pin_hook_installed() -> None:
    """Verify (and re-install) the patched ``socket.getaddrinfo`` resolver.

    The DNS pin works by replacing ``socket.getaddrinfo`` once at module
    import. If anything in the process replaces the symbol *after*
    import — a misbehaving plugin, a test fixture that forgets to
    restore on teardown, an intrusive monitor — the pin silently stops
    taking effect, and the SSRF guard's promise that "the connect goes
    to the IP we validated" goes with it.

    Called at the start of every request. When a replacement is
    detected:

    1. A CRITICAL log message records the tamper event so an operator
       notices something is fighting the safety wrapper.
    2. The replacement is captured as the new ``_ORIG_GETADDRINFO``
       delegate, so any non-pinned lookup still flows through whatever
       the replacement intends (this preserves legitimate uses such as
       a test fixture that installs a fake resolver).
    3. ``_patched_getaddrinfo`` is re-installed at ``socket.getaddrinfo``
       so the pin takes effect again.

    The replacement-as-delegate model means the pin always works
    regardless of what the rest of the process does to
    ``socket.getaddrinfo``. The only way to defeat the pin is to
    replace ``_patched_getaddrinfo`` itself — and that requires being
    able to execute arbitrary code in our process, at which point the
    attacker has already won.
    """
    global _ORIG_GETADDRINFO
    if socket.getaddrinfo is _patched_getaddrinfo:
        return
    logger.critical(
        "socket.getaddrinfo was replaced after http_client import — "
        "DNS pin would not have applied. Capturing the replacement as "
        "the new delegate and re-installing the patched resolver. The "
        "replacement was: %r",
        socket.getaddrinfo,
    )
    # Capture the replacement so non-pinned lookups still flow through it.
    _ORIG_GETADDRINFO = socket.getaddrinfo
    _install_dns_pin_hook()


_install_dns_pin_hook()


@contextlib.contextmanager
def pin_dns_for_request(hostname: str, ip: str):
    """Pin DNS for *hostname* to *ip* for the duration of the ``with`` block.

    The pin is stored on a :class:`threading.local`, so other threads
    are unaffected. On exit the pin is cleared (or restored to the
    previous pin if the context was nested for the same hostname).
    """
    pins: dict[str, str] | None = getattr(_DNS_PIN_LOCAL, "pins", None)
    if pins is None:
        pins = {}
        _DNS_PIN_LOCAL.pins = pins
    previous = pins.get(hostname)
    pins[hostname] = ip
    try:
        yield
    finally:
        if previous is None:
            pins.pop(hostname, None)
        else:
            pins[hostname] = previous


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

#: HTTP status codes for which a server-supplied ``Retry-After`` header
#: is honoured (within reasonable bounds). 429 and 503 are the standard
#: cases; 502/504 do not generally carry a meaningful Retry-After.
_RETRY_AFTER_STATUSES = frozenset({429, 503})

#: Backoff schedule for retries — two extra attempts after the first,
#: so a worst case is three total requests.
_RETRY_BACKOFFS: tuple[float, ...] = (0.5, 1.0)

#: Maximum delay we'll honour for a server-supplied ``Retry-After``
#: header. A pathological upstream cannot pin a worker for an arbitrary
#: amount of time — capped to a sane upper bound.
_RETRY_AFTER_MAX_SECONDS = 60.0

#: Exceptions that we treat as retryable transport-layer errors. Any of
#: these on an idempotent request triggers the backoff loop; the
#: original error is reraised in a :class:`SafeHTTPError` if every
#: retry exhausts.
#:
#: ``Timeout`` and ``ConnectionError`` cover the obvious cases; the
#: remainder cover transient TLS resets, broken chunked transfer, gzip
#: decode races during a server restart, and redirect storms (a
#: redirect loop is harmless to retry once the upstream stabilises).
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.SSLError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    requests.exceptions.TooManyRedirects,
)

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
        # If the caller didn't supply a Session, create a private one so
        # the connection pool is reused across calls. The previous
        # default of ``self._session = None`` fell back to the bare
        # ``requests`` module per request — which works, but pays a
        # full pool-setup cost on every call from module-level
        # singletons (TMDB, OMDb, etc.).
        self._session = session or requests.Session()
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
        """Perform an SSRF-checked GET; retries 429/5xx on GETs by default."""
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

        Args:
            method: HTTP verb (``"GET"``, ``"POST"``, …).
            path_or_url: Absolute URL or a path resolved against
                ``base_url``.
            retry: When ``True``, retry transient transport errors and
                429/5xx responses; honoured automatically for GETs.
            timeout: ``(connect, read)`` tuple, defaulting to
                ``(5.0, 30.0)``.
            max_bytes: Hard cap on the response body, defaulting to
                ``default_max_bytes``.
            headers, params, json, data, auth: Forwarded to the
                underlying ``requests`` call.
            expected_content_type: When set, the response's
                ``Content-Type`` header must start with this value
                (case-insensitive, parameter-stripped). A mismatch
                raises :class:`SafeHTTPError` so a misconfigured
                upstream cannot smuggle HTML / binary into a JSON path.
        """
        # Detect anything that swapped out ``socket.getaddrinfo`` after
        # our import — without this, a tampered resolver would silently
        # disable the DNS pin and reopen the SSRF rebind window.
        _ensure_dns_pin_hook_installed()
        url = self._resolve_url(path_or_url)
        # The SSRF guard validates the URL **and** returns the IP that
        # any subsequent connect must use. Pinning that address inside
        # the dispatch call closes the DNS-rebind window: a hostname
        # that resolved to a public IP here cannot be re-resolved to an
        # internal one when urllib3 actually connects.
        safe, hostname, pinned_ip = resolve_safe_outbound_url(
            url, strict_egress=self._strict_egress
        )
        if not safe:
            raise SafeHTTPError(
                status_code=0,
                body_snippet="refused by SSRF guard",
                url=url,
            )

        timeout = timeout or self._default_timeout
        cap = max_bytes if max_bytes is not None else self._default_max_bytes
        caller = self._session or requests
        attempts = 1 + (len(_RETRY_BACKOFFS) if retry else 0)

        # If the URL was a hostname (not a literal IP) we hold the pin
        # for the entire dispatch + retry loop. A literal-IP URL needs
        # no pin — there's no DNS lookup to corrupt.
        if hostname and pinned_ip:
            with pin_dns_for_request(hostname, pinned_ip):
                return self._dispatch_loop(
                    caller=caller,
                    method=method,
                    url=url,
                    timeout=timeout,
                    cap=cap,
                    attempts=attempts,
                    headers=headers,
                    params=params,
                    json=json,
                    data=data,
                    auth=auth,
                    expected_content_type=expected_content_type,
                )
        return self._dispatch_loop(
            caller=caller,
            method=method,
            url=url,
            timeout=timeout,
            cap=cap,
            attempts=attempts,
            headers=headers,
            params=params,
            json=json,
            data=data,
            auth=auth,
            expected_content_type=expected_content_type,
        )

    def _dispatch_loop(
        self,
        *,
        caller: Any,
        method: str,
        url: str,
        timeout: tuple[float, float],
        cap: int,
        attempts: int,
        headers: dict[str, str] | None,
        params: dict[str, Any] | None,
        json: Any,
        data: Any,
        auth: Any,
        expected_content_type: str | None,
    ) -> requests.Response:
        """Inner dispatch + retry loop.

        Split out from :meth:`_request` so the DNS-pin context can wrap
        the whole loop without forcing a 60-line indent change.
        """
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
            except _RETRYABLE_EXCEPTIONS as exc:
                logger.warning(
                    "HTTP %s %s transport error: %s",
                    method,
                    _safe_path(url),
                    type(exc).__name__,
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
                body = _read_capped(
                    response,
                    cap,
                    expected_content_type=expected_content_type,
                )
            except _SizeCapExceeded as exc:
                response.close()
                raise SafeHTTPError(
                    status_code=response.status_code,
                    body_snippet=str(exc),
                    url=url,
                ) from None
            except _ContentTypeMismatch as exc:
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
                    method,
                    _safe_path(url),
                    response.status_code,
                )
                response.close()
                # Honour ``Retry-After`` on 429/503 within sane bounds;
                # fall back to the fixed backoff schedule for other
                # retryable statuses or when the header is absent /
                # malformed.
                delay = _RETRY_BACKOFFS[attempt]
                if response.status_code in _RETRY_AFTER_STATUSES:
                    advised = _retry_after_seconds(response.headers.get("Retry-After"))
                    if advised is not None:
                        delay = advised
                time.sleep(delay)
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


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into a delay in seconds.

    The header takes either an integer-seconds form (``"30"``) or an
    HTTP-date form (RFC 7231 §7.1.1.1). Returns the delay in seconds
    with a hard cap of :data:`_RETRY_AFTER_MAX_SECONDS` to defeat a
    pathological upstream that asks us to wait for hours. Returns
    ``None`` for missing or unparseable values.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    # Integer-seconds form first — it's the common case and avoids the
    # parsedate_to_datetime overhead.
    try:
        seconds = float(raw)
        if seconds < 0:
            return 0.0
        return min(seconds, _RETRY_AFTER_MAX_SECONDS)
    except (TypeError, ValueError):
        pass
    # HTTP-date form.
    try:
        target = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    # ``parsedate_to_datetime`` returns aware on offset-bearing inputs
    # and naive on missing offsets. Normalise to UTC-aware before diff.
    now = datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = (target - now).total_seconds()
    if delta <= 0:
        return 0.0
    return min(delta, _RETRY_AFTER_MAX_SECONDS)


class _SizeCapExceeded(Exception):
    """Internal signal that the streamed body breached ``max_bytes``."""


class _ContentTypeMismatch(Exception):
    """Internal signal that the response Content-Type was unexpected."""


def _read_capped(
    response: requests.Response,
    max_bytes: int,
    *,
    expected_content_type: str | None = None,
) -> bytes:
    """Read *response* body up to *max_bytes*, raising if the cap is hit.

    Honours an advertised Content-Length as a fast-fail before reading
    anything, then streams chunks to catch servers that omit or lie
    about Content-Length.

    When *expected_content_type* is non-``None``, the response's
    ``Content-Type`` header (case-insensitive, parameter-stripped) is
    matched against it before any body is read, and a mismatch raises
    :class:`_ContentTypeMismatch`. The match is a prefix on the
    ``type/subtype`` portion so ``"application/json"`` matches both
    ``"application/json"`` and ``"application/json; charset=utf-8"``.
    A response advertising ``Content-Encoding`` other than ``identity``
    is rejected because the cap is on decoded bytes and the underlying
    transport may already be decoding — a server that lies about its
    encoding could push past the cap before we see it.
    """
    if expected_content_type:
        ctype_raw = response.headers.get("Content-Type", "") or ""
        # Strip parameters such as charset / boundary.
        ctype = ctype_raw.split(";", 1)[0].strip().lower()
        expected = expected_content_type.split(";", 1)[0].strip().lower()
        if not ctype.startswith(expected):
            raise _ContentTypeMismatch(
                f"unexpected Content-Type {ctype_raw!r}; expected {expected_content_type!r}"
            )
        # ``identity`` (or absent) is the only safe encoding when the
        # caller has pinned a specific content-type — any other value
        # means urllib3 / requests will decode for us, which can defeat
        # the byte cap.
        encoding = (response.headers.get("Content-Encoding", "") or "").strip().lower()
        if encoding and encoding not in ("identity", ""):
            raise _ContentTypeMismatch(
                f"unexpected Content-Encoding {encoding!r}; expected identity"
            )

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
            raise _SizeCapExceeded(f"response body exceeded cap of {max_bytes} bytes")
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
