"""SSRF-aware outbound HTTP layer for mediaman.

This package replaces the monolithic ``services/infra/http_client.py`` with
four focused modules:

* :mod:`.dns_pinning` — monkey-patch and per-thread pin context manager.
* :mod:`.streaming` — size-capped, content-type-validated body reader.
* :mod:`.retry` — Retry-After parsing and backoff orchestration.
* :mod:`.client` — :class:`SafeHTTPClient` and :class:`SafeHTTPError`.

All public names are re-exported here so ``from mediaman.services.infra.http
import SafeHTTPClient`` works, and the back-compat shim in ``http_client.py``
preserves every pre-split import path.
"""

from mediaman.services.infra.http.client import (
    _DEFAULT_MAX_BYTES,
    _DEFAULT_TIMEOUT_SECONDS,
    _USER_AGENT,
    SafeHTTPClient,
    SafeHTTPError,
    _dispatch,
)
from mediaman.services.infra.http.dns_pinning import (
    _ORIG_GETADDRINFO,
    _patched_getaddrinfo,
    ensure_hook_installed,
    pin,
    pin_dns_for_request,
)
from mediaman.services.infra.http.retry import (
    _BODY_SNIPPET_BYTES,
    _RETRY_AFTER_MAX_SECONDS,
    _RETRY_AFTER_STATUSES,
    _RETRY_BACKOFFS,
    _RETRYABLE_EXCEPTIONS,
    _RETRYABLE_STATUSES,
    _retry_after_seconds,
)
from mediaman.services.infra.http.streaming import (
    _ContentTypeMismatch,
    _read_capped,
    _SizeCapExceeded,
)

__all__ = [
    "_BODY_SNIPPET_BYTES",
    "_DEFAULT_MAX_BYTES",
    "_DEFAULT_TIMEOUT_SECONDS",
    "_ORIG_GETADDRINFO",
    "_RETRYABLE_EXCEPTIONS",
    "_RETRYABLE_STATUSES",
    "_RETRY_AFTER_MAX_SECONDS",
    "_RETRY_AFTER_STATUSES",
    "_RETRY_BACKOFFS",
    "_USER_AGENT",
    "SafeHTTPClient",
    "SafeHTTPError",
    "_ContentTypeMismatch",
    "_SizeCapExceeded",
    "_dispatch",
    "_patched_getaddrinfo",
    "_read_capped",
    "_retry_after_seconds",
    "ensure_hook_installed",
    "pin",
    "pin_dns_for_request",
]
