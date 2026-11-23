"""TMDB images for Watch, served from the disk cache so the browser never calls TMDB.

Only a known size and a plain TMDB file name are accepted, and the upstream fetch goes to
image.tmdb.org alone, capped in size, and must answer with an allowed image type. Anyone
signed in may load them; the per-person limit counts only fetches from TMDB, since a
browse page shows a hundred posters or more from the cache.
"""

from __future__ import annotations

import logging
import re

import requests
from fastapi import APIRouter, Depends
from starlette.responses import Response

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError, disk_cache
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.routes.poster.cache import ALLOWED_IMAGE_MIMES

logger = logging.getLogger(__name__)

router = APIRouter()

_SIZES = frozenset({"w185", "w342", "w500", "w780", "w1280"})
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\.(jpg|jpeg|png|webp)")

# TMDB never changes the image behind a path.
_FRESH_S = 90 * 86400
_STALE_S = 365 * 86400
_CACHE_CONTROL = "private, max-age=2592000, immutable"

_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"
_TMDB_HTTP = SafeHTTPClient(
    default_timeout=(3.0, 5.0),
    default_max_bytes=4 * 1024 * 1024,
    allowed_hosts=frozenset({"image.tmdb.org"}),
)

_IMG_LIMITER = ActionRateLimiter(max_in_window=240, window_seconds=60)


def _fetch_tmdb_image(size: str, name: str) -> tuple[bytes, str]:
    """Fetch one TMDB image; raises SafeHTTPError when it fails or is not an allowed image."""
    url = f"{_TMDB_IMAGE_BASE}/{size}/{name}"
    response = _TMDB_HTTP.get(url, expected_content_type="image/")
    mime = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if mime not in ALLOWED_IMAGE_MIMES:
        raise SafeHTTPError(415, f"unexpected content type {mime[:40]}", url)
    return response.content, mime


@router.get("/api/watch/img/tmdb/{size}/{name}")
def api_watch_tmdb_image(
    size: str, name: str, username: str = Depends(get_current_user)
) -> Response:
    if size not in _SIZES or not _NAME_RE.fullmatch(name):
        return Response(status_code=404)

    def fetch() -> disk_cache.Image:
        content, mime = _fetch_tmdb_image(size, name)
        return content, {"mime": mime}

    try:
        content, info = disk_cache.img().get_or_fetch(
            f"img:tmdb:{size}:{name}",
            _FRESH_S,
            _STALE_S,
            fetch,
            allow_fetch=lambda: _IMG_LIMITER.check(username),
        )
    except disk_cache.Refused:
        return Response(status_code=429)
    except SafeHTTPError as exc:
        if exc.status_code == 404:
            return Response(status_code=404)
        logger.warning("TMDB image fetch failed (%s)", exc.status_code)
        return Response(status_code=503)
    except requests.RequestException:
        logger.warning("TMDB image fetch failed", exc_info=True)
        return Response(status_code=503)
    mime = info.get("mime", "")
    return Response(
        content=content,
        media_type=mime if mime in ALLOWED_IMAGE_MIMES else "image/jpeg",
        headers={"Cache-Control": _CACHE_CONTROL},
    )


__all__ = ["router"]
