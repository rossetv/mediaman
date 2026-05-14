"""The :class:`SafeHTTPClient` class — the public outbound HTTP surface.

This module owns the client class and its near-identical
``get``/``post``/``put``/``delete`` verb methods plus the ``_request``
orchestration that threads each call through the safety machinery. The
low-level transport, the per-call SSRF / dispatch indirection helpers, and
the timeout / size-cap defaults live in :mod:`._request`; the error type
lives in :mod:`._errors`.

``_dispatch`` and ``resolve_safe_outbound_url`` are resolved at call time
through the ``mediaman.services.infra.http.client`` package namespace (via
``sys.modules``) — see :mod:`._request` for why that seam has to be dynamic.
"""

from __future__ import annotations

import contextlib
from typing import Any, Literal

import requests

from mediaman.services.infra.http.client._errors import SafeHTTPError
from mediaman.services.infra.http.client._request import (
    _DEFAULT_MAX_BYTES,
    _DEFAULT_TIMEOUT_SECONDS,
    _USER_AGENT,
    _invoke_dispatch,
    _resolve_outbound,
)
from mediaman.services.infra.http.dns_pinning import ensure_hook_installed, pin
from mediaman.services.infra.http.retry import _RETRY_BACKOFFS, dispatch_loop
from mediaman.services.infra.http.streaming import _read_capped

# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class SafeHTTPClient:
    """SSRF- and size-aware wrapper around :mod:`requests`.

    Instantiate one per logical outbound target so the connection pool
    (``session``) can be reused.  Every call re-validates the target URL via
    :func:`~mediaman.services.infra.url_safety.resolve_safe_outbound_url`,
    which re-resolves DNS — this is the DNS-rebind defence.

    When *allowed_hosts* is non-``None``, the URL's IDN-normalised hostname
    must additionally appear in that set (or in
    :data:`~mediaman.services.infra.url_safety.PINNED_EXTERNAL_HOSTS`) for
    the call to proceed. The deny-list still applies on top — an
    allowlisted host that resolves to a metadata IP is still refused. When
    ``None`` (the default), only the deny-list is consulted. Callers
    typically derive the set once at the boundary via
    :func:`~mediaman.services.infra.url_safety.allowed_outbound_hosts`.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        session: requests.Session | None = None,
        default_timeout: tuple[float, float] = _DEFAULT_TIMEOUT_SECONDS,
        default_max_bytes: int = _DEFAULT_MAX_BYTES,
        allowed_hosts: frozenset[str] | None = None,
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
        self._allowed_hosts = allowed_hosts

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
        jitter_strategy: Literal["fixed", "full"] = "fixed",
        abort_after_consecutive_5xx: int | None = None,
        retryable_statuses: frozenset[int] | None = None,
    ) -> requests.Response:
        """Perform an SSRF-checked POST; no retries unless ``retry=True``.

        ``jitter_strategy``, ``abort_after_consecutive_5xx``, and
        ``retryable_statuses`` thread through to :func:`dispatch_loop`
        so retry-heavy callers (e.g. the mailgun POST path) can opt into
        full-jitter backoff and an early-abort policy without rolling
        their own retry primitive.
        """
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
            jitter_strategy=jitter_strategy,
            abort_after_consecutive_5xx=abort_after_consecutive_5xx,
            retryable_statuses=retryable_statuses,
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
        jitter_strategy: Literal["fixed", "full"] = "fixed",
        abort_after_consecutive_5xx: int | None = None,
        retryable_statuses: frozenset[int] | None = None,
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

        # SSRF re-validation on every call (re-resolves DNS — the rebind
        # defence). ``_resolve_outbound`` performs the ``sys.modules`` lookup
        # of the guard so tests can patch it; see its docstring.
        safe, hostname, pinned_ip = _resolve_outbound(url, self._allowed_hosts)
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
            return _invoke_dispatch(
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
            return _read_capped(response, cap, expected_content_type=expected_content_type)

        ctx = pin(hostname, pinned_ip) if (hostname and pinned_ip) else contextlib.nullcontext()
        with ctx:
            return dispatch_loop(
                dispatch_fn=_dispatch_fn,
                read_fn=_read_fn,
                method=method,
                url=url,
                attempts=attempts,
                make_error=SafeHTTPError,
                jitter_strategy=jitter_strategy,
                abort_after_consecutive_5xx=abort_after_consecutive_5xx,
                retryable_statuses=retryable_statuses,
            )
