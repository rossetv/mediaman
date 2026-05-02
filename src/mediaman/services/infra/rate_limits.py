"""Shared rate-limiter instances used across multiple route modules.

Centralising these here prevents the same limiter being declared twice
(once in subscribers.py, once in settings.py) with potentially different
parameters that silently diverge over time.
"""

from __future__ import annotations

from mediaman.auth.rate_limit import ActionRateLimiter, RateLimiter

# Newsletter send limiter — shared by subscribers.py (/api/newsletter/send).
# 3 sends per 5 minutes per admin, 10 per day.
NEWSLETTER_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=300, max_per_day=10)

# Settings write limiter — shared by settings.py and settings/crud.py.
# The two modules previously declared the same parameters independently;
# this single instance is the canonical version.
SETTINGS_WRITE_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60, max_per_day=200)

# Service-test limiter for /api/settings/test/{service} — bounded so a
# logged-in admin (or an attacker who lifted a session cookie) cannot
# chain tests to flood Plex / Mailgun. 10 per minute, 60 per day per
# actor — generous for an operator clicking through every Test button
# during configuration but tight enough to kill abuse.
SETTINGS_TEST_LIMITER = ActionRateLimiter(max_in_window=10, window_seconds=60, max_per_day=60)

# Subscriber add/remove limiter for the admin /api/subscribers endpoints.
# Looser than the newsletter-send limiter (which spawns mail) but still
# bounded so a compromised admin token cannot script thousands of
# add/remove operations. 5 per minute, 50 per day per actor.
SUBSCRIBER_WRITE_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=50)

# Manual-scan trigger limiter for /api/scan/trigger and the related
# admin-initiated scan operations. Manual scans are heavy (Plex
# round-trips + Sonarr/Radarr fan-out), so a much tighter cap than the
# settings limiter: 3 per minute, 20 per day per actor.
SCAN_TRIGGER_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=60, max_per_day=20)

# IP-bucketed limiter for the unauthenticated signed-poster path. The
# signed token already gates access to a single rating key, but a leaked
# URL otherwise has no rate cap — bound it at 60 requests per minute
# per /24 (IPv4) or /64 (IPv6) bucket so a leaked URL cannot be used as
# a bandwidth-amplification vector against the proxy.
POSTER_PUBLIC_LIMITER = RateLimiter(max_attempts=60, window_seconds=60)
