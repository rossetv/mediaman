"""TMDB images for a page or mail opened with no session: the mailed download page and the mails.

Each address carries a signature from sign_tmdb_image_url binding the size, the file name and
an expiry, so it fetches that one picture and nothing else. A missing, wrong or expired
signature, or an address no signature could name, is a uniform 401, as /api/poster answers.
Only refusals count against a per-network limit, and a network over it gets 429 for them: a
good signature names a picture the app itself signed, served from the disk cache after its
first fetch, so it is never turned away, whoever else shares the network.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from mediaman.crypto import validate_tmdb_image_token
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.routes.watch.image_api import is_tmdb_image, tmdb_image_response

router = APIRouter()

_PUBLIC_LIMITER = RateLimiter(max_attempts=60, window_seconds=60)
_MAX_SIG_LEN = 4096


@router.get("/api/img/tmdb/{size}/{name}")
async def api_signed_tmdb_image(
    request: Request, size: str, name: str, sig: str | None = None
) -> Response:
    payload = (
        validate_tmdb_image_token(sig, request.app.state.config.secret_key)
        if sig and len(sig) <= _MAX_SIG_LEN and is_tmdb_image(size, name)
        else None
    )
    if payload is None or payload.get("img") != f"{size}/{name}":
        if not _PUBLIC_LIMITER.check(get_client_ip(request)):
            return Response(status_code=429)
        return Response(status_code=401)
    return await tmdb_image_response(size, name, lambda: True)


__all__ = ["router"]
