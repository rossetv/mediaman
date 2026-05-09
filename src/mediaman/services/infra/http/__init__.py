"""SSRF-aware outbound HTTP layer for mediaman.

This package replaces the monolithic ``services/infra/http_client.py`` with
four focused modules:

* :mod:`.dns_pinning` — monkey-patch and per-thread pin context manager.
* :mod:`.streaming` — size-capped, content-type-validated body reader.
* :mod:`.retry` — Retry-After parsing and backoff orchestration.
* :mod:`.client` — :class:`SafeHTTPClient` and :class:`SafeHTTPError`.

Only the legitimate public surface is re-exported here; callers that need
implementation-detail names import them from the canonical sub-module.
"""

from mediaman.services.infra.http.client import SafeHTTPClient, SafeHTTPError
from mediaman.services.infra.http.dns_pinning import (
    ensure_hook_installed,
    pin,
    pin_dns_for_request,
)

__all__ = [
    "SafeHTTPClient",
    "SafeHTTPError",
    "ensure_hook_installed",
    "pin",
    "pin_dns_for_request",
]
