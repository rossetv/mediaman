"""SSRF-aware outbound HTTP layer for mediaman.

Four focused modules make up the layer:

* ``dns_pinning`` — monkey-patch and per-thread pin context manager.
* ``streaming`` — size-capped, content-type-validated body reader.
* ``retry`` — Retry-After parsing and backoff orchestration.
* ``client`` — ``SafeHTTPClient`` and ``SafeHTTPError``.

Only the legitimate public surface is re-exported here; callers that need
implementation-detail names import them from the canonical sub-module.
"""

from __future__ import annotations

from mediaman.services.infra.http.client import SafeHTTPClient, SafeHTTPError
from mediaman.services.infra.http.dns_pinning import (
    ensure_hook_installed,
    pin,
)

__all__ = [
    "SafeHTTPClient",
    "SafeHTTPError",
    "ensure_hook_installed",
    "pin",
]
