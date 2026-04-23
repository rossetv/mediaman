"""Rate-limiting package — IP-aware limiters and client-IP resolution.

Split from the original monolithic ``rate_limit.py`` (R4). Callers
continue to import every symbol from :mod:`mediaman.auth.rate_limit`.
"""
# ruff: noqa: F401 — this module is a deliberate re-export facade; the
# "unused" private imports are part of the module's public surface.

from .ip_resolver import (
    _ip_in_networks,
    get_client_ip,
    peer_is_trusted,
    trusted_proxies,
)
from .limiters import (
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
