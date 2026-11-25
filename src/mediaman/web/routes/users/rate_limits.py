"""Module-level rate limiter instances for user-management routes.

Every limiter lives here so route modules share one canonical instance and tests can reset
them all from one place.

_USER_MGMT_LIMITER: general operations (delete, unlock); wide enough for real admin
workflows, tight enough to contain a stolen session.

_UNTICKETED_LIMITER: per-actor cap (30/min, as reauth) on tries of either kind without a recent
reauth ticket, which use up neither limit. No daily cap: it would keep out for a day a person who
has yet to be asked for the password.

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

import math

from fastapi.responses import JSONResponse

from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.responses import respond_err

_USER_MGMT_LIMITER = ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)
_USER_CREATE_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=3600, max_per_day=5)
_UNTICKETED_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60)
_REAUTH_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
_PASSWORD_CHANGE_LIMITER = ActionRateLimiter(max_in_window=10, window_seconds=60, max_per_day=100)
_PASSWORD_CHANGE_IP_LIMITER = ActionRateLimiter(
    max_in_window=10, window_seconds=60, max_per_day=200
)
_SESSIONS_LIST_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)
_SESSIONS_REVOKE_LIMITER = ActionRateLimiter(max_in_window=10, window_seconds=60, max_per_day=100)


def _wait_text(seconds: int) -> str:
    count, unit = seconds, "second"
    if seconds >= 60:
        count, unit = math.ceil(seconds / 60), "minute"
    if count >= 60:
        count, unit = math.ceil(seconds / 3600), "hour"
    return f"{count} {unit}{'' if count == 1 else 's'}"


def too_many(
    limiter: ActionRateLimiter, actor: str, what: str = "Too many changes."
) -> JSONResponse:
    """The refusal of a try over the limit, with how long *actor* has to wait."""
    seconds = limiter.retry_after(actor)
    resp = respond_err(
        "too_many_requests", status=429, message=f"{what} Try again in {_wait_text(seconds)}."
    )
    resp.headers["Retry-After"] = str(seconds)
    return resp
