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

Package layout
--------------
This package decomposes the former single ``client.py`` along the seam the
``services-infra`` audit named:

* :mod:`._errors` — the :class:`SafeHTTPError` exception type.
* :mod:`._request` — the single-attempt transport (:func:`_dispatch`) and the
  per-call SSRF / dispatch indirection helpers, plus the timeout / size-cap
  defaults.
* :mod:`._core` — the :class:`SafeHTTPClient` class, its verb methods, and the
  ``_request`` orchestration.

This module is the public barrel: ``SafeHTTPClient``, ``SafeHTTPError``, and
the patch-seam names (``_dispatch``, ``resolve_safe_outbound_url``, ``time``)
are re-exported here so every name previously importable from
``mediaman.services.infra.http.client`` stays importable from that exact path.

Patchability note
-----------------
``_dispatch`` and ``resolve_safe_outbound_url`` are resolved at call time
through the ``mediaman.services.infra.http.client`` module namespace (via
``sys.modules``) rather than via a static import binding.  This is required
so that ``monkeypatch.setattr(http_client, "_dispatch", ...)`` in tests
intercepts the actual transport call — otherwise pytest's monkeypatch would
change the name in the wrong module dict and the original function would still
be invoked.  The same applies to ``resolve_safe_outbound_url``.  Both names
are re-exported below (and ``sys.modules`` resolves ``_HTTP_CLIENT_MODULE`` to
*this* package module), so the dynamic lookup in :mod:`._request` finds the
patched object.  ``time`` is re-exported for the same reason — tests patch
``http_client.time.sleep`` and the retry loop sleeps on the shared module.
"""

from __future__ import annotations

import logging
import time  # noqa: F401 — tests patch http.client.time.sleep via monkeypatch

from mediaman.services.infra.http.client._core import SafeHTTPClient
from mediaman.services.infra.http.client._errors import SafeHTTPError
from mediaman.services.infra.http.client._request import (
    _DEFAULT_MAX_BYTES,
    _DEFAULT_TIMEOUT_SECONDS,
    _HTTP_CLIENT_MODULE,
    _USER_AGENT,
    _build_user_agent,
    _dispatch,
    _invoke_dispatch,
    _resolve_outbound,
    resolve_safe_outbound_url,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_DEFAULT_MAX_BYTES",
    "_DEFAULT_TIMEOUT_SECONDS",
    "_HTTP_CLIENT_MODULE",
    "_USER_AGENT",
    "SafeHTTPClient",
    "SafeHTTPError",
    "_build_user_agent",
    "_dispatch",
    "_invoke_dispatch",
    "_resolve_outbound",
    "resolve_safe_outbound_url",
]
