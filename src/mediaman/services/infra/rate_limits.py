"""Shared rate-limiter instances used across multiple route modules.

Centralising these here prevents the same limiter being declared twice
(once in subscribers.py, once in settings.py) with potentially different
parameters that silently diverge over time.
"""

from __future__ import annotations

from mediaman.auth.rate_limit import ActionRateLimiter

# Newsletter send limiter — shared by subscribers.py (/api/newsletter/send).
# 3 sends per 5 minutes per admin, 10 per day.
NEWSLETTER_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=300, max_per_day=10)

# Settings write limiter — shared by settings.py and settings/crud.py.
# The two modules previously declared the same parameters independently;
# this single instance is the canonical version.
SETTINGS_WRITE_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60, max_per_day=200)
