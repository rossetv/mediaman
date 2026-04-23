"""Shared rate-limiter instances used across multiple route modules.

Centralising these here prevents the same limiter being declared twice
(once in subscribers.py, once in settings.py) with potentially different
parameters that silently diverge over time.
"""

from mediaman.auth.rate_limit import ActionRateLimiter

# Newsletter send limiter — shared by subscribers.py (/api/newsletter/send)
# and settings.py (if it ever exposes a send path).  3 sends per 5 minutes
# per admin, 10 per day.
NEWSLETTER_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=300, max_per_day=10)
