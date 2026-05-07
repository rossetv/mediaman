"""Domain-agnostic rate-limiting package.

Relocated from ``mediaman.auth.rate_limit`` (R-refactor) because the
limiters are not inherently auth-specific — they guard API endpoints in
scanner, settings, library, and subscriber routes as well.

A thin back-compat shim remains at :mod:`mediaman.auth.rate_limit` so
existing imports continue to work without modification.

Public surface
--------------
``RateLimiter``
    IP-bucketed sliding-window limiter for unauthenticated routes.
``ActionRateLimiter``
    Per-actor limiter for authenticated admin operations.
``get_client_ip``
    Real-IP extraction respecting trusted-proxy forwarded headers.
``peer_is_trusted``
    Predicate for trusted-proxy allowlist membership.
``trusted_proxies``
    Parsed ``MEDIAMAN_TRUSTED_PROXIES`` allowlist (cached).

Note: the ``rate_limit`` decorator is intentionally NOT re-exported here.
It is FastAPI-coupled (uses ``Request`` and ``respond_err`` from the web
layer) and therefore lives in :mod:`mediaman.web.middleware.rate_limit`.
Import it directly from that module using the full dotted path.
"""

# ruff: noqa: F401 — deliberate re-export facade.

from mediaman.services.rate_limit.ip_resolver import (
    _ip_in_networks,
    get_client_ip,
    peer_is_trusted,
    trusted_proxies,
)
from mediaman.services.rate_limit.limiters import (
    _MAX_BUCKETS,
    ActionRateLimiter,
    RateLimiter,
    _bucket_key,
)

__all__ = [
    "ActionRateLimiter",
    "RateLimiter",
    "get_client_ip",
    "peer_is_trusted",
    "trusted_proxies",
]
