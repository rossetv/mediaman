"""Back-compat shim — rate-limiting package moved to :mod:`mediaman.services.rate_limit`.

All symbols previously importable from ``mediaman.web.auth.rate_limit`` are
re-exported here unchanged so existing callers need no modification.
New code should import from :mod:`mediaman.services.rate_limit` directly.
"""

# ruff: noqa: F401 — deliberate re-export facade.

# Expose the sub-modules under the old path so code that does
# ``from mediaman.web.auth.rate_limit import ip_resolver as ip_resolver_module``
# (as in tests/unit/auth/test_rate_limit.py) continues to work.
from mediaman.services.rate_limit import (
    ActionRateLimiter,
    RateLimiter,
    get_client_ip,
    ip_resolver,
    limiters,
    peer_is_trusted,
    rate_limit,
    trusted_proxies,
)
from mediaman.services.rate_limit.ip_resolver import (
    _ip_in_networks,
    clear_cache,
    cloudflare_proxies,
)
from mediaman.services.rate_limit.limiters import (
    _MAX_BUCKETS,
    _bucket_key,
)

__all__ = [
    "ActionRateLimiter",
    "RateLimiter",
    "get_client_ip",
    "ip_resolver",
    "limiters",
    "peer_is_trusted",
    "rate_limit",
    "trusted_proxies",
]
