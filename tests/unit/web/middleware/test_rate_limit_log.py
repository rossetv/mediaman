from __future__ import annotations

from fastapi import Request

from mediaman.services.rate_limit.limiters import RateLimiter
from mediaman.web.middleware.rate_limit import rate_limit


def test_throttled_actor_cannot_forge_a_log_line(caplog):
    limiter = RateLimiter(max_attempts=1, window_seconds=60)

    @rate_limit(limiter)
    def handler(admin: str, request: Request):
        return "ok"

    name = "x\nauth.login_ok user=root"
    handler(admin=name, request=None)
    with caplog.at_level("WARNING"):
        handler(admin=name, request=None)
    msg = next(r.getMessage() for r in caplog.records if "rate_limit.throttled" in r.getMessage())
    assert "\n" not in msg
