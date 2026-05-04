"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.rate_limit.limiters`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.rate_limit.limiters as _real
from mediaman.web.auth.rate_limit.limiters import (
    _MAX_BUCKETS,
    ActionRateLimiter,
    RateLimiter,
    _bucket_key,
)

sys.modules[__name__] = _real
