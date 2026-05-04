"""Back-compat shim — rate limiters moved to :mod:`mediaman.services.rate_limit.limiters`.

Re-exports everything from the canonical location so existing imports
continue to work without modification.
"""

# ruff: noqa: F401 — deliberate re-export facade.

from mediaman.services.rate_limit.limiters import (
    _MAX_BUCKETS,
    ActionRateLimiter,
    RateLimiter,
    _bucket_key,
)

__all__ = [
    "ActionRateLimiter",
    "RateLimiter",
]
