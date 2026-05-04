"""Module-level rate limiter instances for user-management routes.

Every limiter lives here so that:
- route modules import a single canonical instance (no drift between copies).
- tests can import and reset them all from one place.

Limiter rationale
-----------------
``_USER_MGMT_LIMITER``
    General user-management operations (delete, unlock). Wide enough for
    legitimate admin workflows; tight enough to contain a stolen-session
    attacker.

``_USER_CREATE_LIMITER``
    Tighter than the general limiter — a compromised admin session must
    not be able to mass-create accounts before being spotted. 3 per hour
    / 5 per day is sufficient for any legitimate operator workflow.

``_REAUTH_LIMITER``
    Per-actor per-minute cap on reauth attempts. The namespace lockout
    inside ``verify_reauth_password`` is the main brute-force defence;
    this limiter is the per-minute throttle that keeps the lockout
    reachable without allowing an open-ended number of bcrypt cycles.
    30/minute leaves head-room for the lockout to trip at 5 failures and
    then continue climbing toward the 10/15 escalation bands.

``_PASSWORD_CHANGE_LIMITER``
    Per-actor burst cap for password-change calls (10/min). Tighter than
    the reauth limiter because a successful change rotates the
    credential — no legitimate workflow fires this endpoint dozens of
    times per minute. Set just above the 5-failures namespace lockout
    threshold so the ``403 account locked`` surfaces before the ``429``.

``_PASSWORD_CHANGE_IP_LIMITER``
    Per-IP companion to ``_PASSWORD_CHANGE_LIMITER``. A stolen session
    cookie can be replayed from anywhere; the per-actor bucket alone does
    not cover that fan-out. A per-IP cap applied in addition prevents a
    single source grinding away even if the username rotates at the
    request layer.

``_SESSIONS_LIST_LIMITER``
    Per-actor cap for ``GET /api/users/sessions``. Without this an
    attacker holding a stolen cookie could poll the endpoint to detect
    the moment the legitimate user signs in (a new row appears). 30/min
    leaves comfortable head-room for a UI that refreshes every few
    seconds.
"""

from __future__ import annotations

from mediaman.auth.rate_limit import ActionRateLimiter

_USER_MGMT_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)
_USER_CREATE_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=3600, max_per_day=5)
_REAUTH_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
_PASSWORD_CHANGE_LIMITER = ActionRateLimiter(max_in_window=10, window_seconds=60, max_per_day=100)
_PASSWORD_CHANGE_IP_LIMITER = ActionRateLimiter(
    max_in_window=10, window_seconds=60, max_per_day=200
)
_SESSIONS_LIST_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
