"""Back-compat shim — rate-limiting package moved to :mod:`mediaman.web.auth.rate_limit`.

All symbols previously importable from ``mediaman.auth.rate_limit`` are
re-exported here unchanged so existing callers need no modification.
New code should import from :mod:`mediaman.services.rate_limit` directly.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.rate_limit as _real
from mediaman.web.auth.rate_limit import (
    _MAX_BUCKETS,
    ActionRateLimiter,
    RateLimiter,
    _bucket_key,
    _ip_in_networks,
    clear_cache,
    cloudflare_proxies,
    get_client_ip,
    ip_resolver,
    limiters,
    peer_is_trusted,
    rate_limit,
    trusted_proxies,
)

sys.modules[__name__] = _real
