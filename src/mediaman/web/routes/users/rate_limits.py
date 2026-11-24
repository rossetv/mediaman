"""Module-level rate limiter instances for user-management routes.

Every limiter lives here so route modules share one canonical instance and tests can reset
them all from one place.

_USER_MGMT_LIMITER: general operations (delete, unlock); wide enough for real admin
workflows, tight enough to contain a stolen session.

_USER_CREATE_LIMITER: tighter (3/hour, 5/day) so a compromised session cannot mass-create
accounts before being spotted.

_REAUTH_LIMITER: per-actor per-minute cap on reauth attempts. The namespace lockout is the
main brute-force defence; this keeps it reachable without allowing unbounded bcrypt cycles.
30/minute lets the lockout trip at 5 failures and keep climbing toward the 10/15 escalation
bands.

_PASSWORD_CHANGE_LIMITER: per-actor cap (10/min), set just above the 5-failure lockout
threshold so "403 account locked" surfaces before the 429.

_PASSWORD_CHANGE_IP_LIMITER: per-IP companion, since a stolen cookie can be replayed from
anywhere and a per-actor bucket alone does not cover that fan-out.

_SESSIONS_LIST_LIMITER: per-actor cap (30/min) on GET /api/users/sessions, so a cookie thief
cannot poll it to detect the moment the legitimate user signs in.

_SESSIONS_REVOKE_LIMITER: per-actor cap (10/min) on POST /api/users/sessions/revoke-others,
which writes sessions and an audit row on each call.
"""

from __future__ import annotations

from mediaman.services.rate_limit import ActionRateLimiter

_USER_MGMT_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)
_USER_CREATE_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=3600, max_per_day=5)
_REAUTH_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
_PASSWORD_CHANGE_LIMITER = ActionRateLimiter(max_in_window=10, window_seconds=60, max_per_day=100)
_PASSWORD_CHANGE_IP_LIMITER = ActionRateLimiter(
    max_in_window=10, window_seconds=60, max_per_day=200
)
_SESSIONS_LIST_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
_SESSIONS_REVOKE_LIMITER = ActionRateLimiter(max_in_window=10, window_seconds=60, max_per_day=100)
