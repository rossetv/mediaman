"""Proxy Plex poster images with on-disk caching.

Keeps the Plex token out of the frontend.  Posters are cached to
``MEDIAMAN_DATA_DIR/poster_cache/`` on first fetch and served from disk
on subsequent requests, avoiding repeated round-trips to Plex.

Package layout
--------------
``poster/cache.py``
    Filesystem cache state and helpers: directory bootstrap, atomic
    write, LRU sweep, sidecar mime persistence.

``poster/_validation.py``
    Pure validation helpers: rating-key validation, mime coercion,
    allowed-host check, and Plex URL sanitiser.

``poster/fetch.py``
    Outbound HTTP fetch logic and SSRF-allowlist client construction
    for Plex and the Radarr/Sonarr fallback path.

``poster/__init__.py`` (this module)
    FastAPI route handler, rate limiters, auth gate, and back-compat
    re-exports of the names tests patch at the
    ``mediaman.web.routes.poster.<name>`` path.

Access control
--------------

Logged-in admins can fetch any poster by rating key.  Email clients
have no session cookie, so email-embedded posters must be rendered
with a signed URL produced by :func:`sign_poster_url`.  The signature
carries an expiry (default 180 days) and is domain-separated via a
dedicated HMAC sub-key (see :mod:`mediaman.crypto`).

Unauthenticated callers (no session, no valid signed token) receive
a uniform 401 regardless of whether the rating_key exists on the
Plex server.  This prevents the endpoint being used as an existence
oracle to enumerate the user's library rating keys.
"""

from __future__ import annotations

import hashlib
import logging
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from mediaman.crypto import (
    sign_poster_url,
    validate_poster_token,
)
from mediaman.db import get_db
from mediaman.services.rate_limit import get_client_ip
from mediaman.services.rate_limit.instances import (
    POSTER_PUBLIC_LIMITER as _POSTER_PUBLIC_LIMITER,
)
from mediaman.web.auth.middleware import get_optional_admin
from mediaman.web.routes.poster._validation import (
    is_allowed_poster_host as _is_allowed_poster_host,
)
from mediaman.web.routes.poster._validation import (
    is_valid_rating_key as _validate_rating_key,
)
from mediaman.web.routes.poster._validation import (
    safe_mime as _safe_mime,
)
from mediaman.web.routes.poster.cache import (
    CACHE_MAX_AGE_SECONDS as _CACHE_MAX_AGE_SECONDS,
)
from mediaman.web.routes.poster.cache import (
    get_cache_dir as _get_cache_dir,
)
from mediaman.web.routes.poster.cache import (
    read_sidecar_mime as _read_sidecar_mime,
)
from mediaman.web.routes.poster.cache import (
    write_poster_cache as _write_poster_cache,
)
from mediaman.web.routes.poster.fetch import (
    _POSTER_HTTP,
)
from mediaman.web.routes.poster.fetch import (
    fetch_arr_poster as _fetch_arr_poster,
)
from mediaman.web.routes.poster.fetch import (
    load_plex_credentials as _load_plex_credentials,
)
from mediaman.web.routes.poster.fetch import (
    resolve_poster_content as _resolve_poster_content,
)

__all__ = [
    "_CACHE_MAX_AGE_SECONDS",
    "_POSTER_HTTP",
    "_POSTER_PUBLIC_LIMITER",
    "_fetch_arr_poster",
    "_is_allowed_poster_host",
    "_read_sidecar_mime",
    "_safe_mime",
    "_validate_rating_key",
    "_write_poster_cache",
    "proxy_poster",
    "router",
    "sign_poster_url",
]

logger = logging.getLogger(__name__)

router = APIRouter()


def _authenticate_poster_request(
    request: Request,
    rating_key: str,
    sig: str | None,
    admin: str | None,
    secret_key: str,
) -> Response | None:
    """Authenticate a poster request.  Return an error Response or None on success.

    Auth FIRST — return 401 for any unauthenticated caller regardless
    of whether the rating_key is well-formed or present on the Plex
    server.  This closes the enumeration oracle noted in the pentest.
    Authenticated admin sessions skip the IP cap — they're already
    bounded by the auth layer.
    """
    if admin is None:
        if not _POSTER_PUBLIC_LIMITER.check(get_client_ip(request)):
            return Response(status_code=429)
        if not sig or len(sig) > 4096:
            return Response(status_code=401)
        if not _validate_rating_key(rating_key):
            return Response(status_code=401)
        poster_payload = validate_poster_token(sig, secret_key)
        if poster_payload is None or poster_payload.get("rk") != rating_key:
            return Response(status_code=401)
    else:
        if not _validate_rating_key(rating_key):
            return Response(status_code=404)
    return None


@router.get("/api/poster/{rating_key}")
def proxy_poster(
    request: Request,
    rating_key: str,
    sig: str | None = None,
    admin: str | None = Depends(get_optional_admin),
) -> Response:
    """Serve a poster image, fetching from Plex only on cache miss.

    Authentication: either an active admin session, or a valid HMAC
    signature in ``?sig=...`` (generated via :func:`sign_poster_url`).
    Unauthenticated callers with no or bad signature get a 401 —
    importantly, the 401 is returned BEFORE any rating_key validity
    or existence check, so the endpoint cannot be used as an oracle
    to enumerate Plex rating keys.

    Unauthenticated callers (signed-URL path) are additionally
    rate-limited per /24 (IPv4) or /64 (IPv6) at 60 req/min so a leaked
    signed URL cannot be used as a bandwidth-amplification vector.
    Authenticated admin sessions skip the IP cap — they're already
    bounded by the auth layer.
    """
    config = request.app.state.config
    auth_err = _authenticate_poster_request(request, rating_key, sig, admin, config.secret_key)
    if auth_err is not None:
        return auth_err

    cache_dir = _get_cache_dir(config.data_dir)
    # Full SHA-256 cache filename — collision-free, filesystem-safe.
    cached_path = cache_dir / f"{hashlib.sha256(rating_key.encode()).hexdigest()}.jpg"

    # Serve from disk cache (sidecar carries the true mime type).
    if cached_path.exists():
        return Response(
            content=cached_path.read_bytes(),
            media_type=_read_sidecar_mime(cached_path),
            headers={"Cache-Control": f"public, max-age={_CACHE_MAX_AGE_SECONDS}"},
        )

    conn = get_db()
    plex_base, plex_token, cred_err = _load_plex_credentials(conn, config.secret_key)
    if cred_err is not None:
        return cred_err

    content, content_type, fetch_err = _resolve_poster_content(
        conn, rating_key, cast(str, plex_base), cast(str, plex_token), config
    )
    if fetch_err is not None:
        return fetch_err

    poster_bytes = cast(bytes, content)
    _write_poster_cache(cache_dir, cached_path, rating_key, poster_bytes, content_type)
    return Response(
        content=poster_bytes,
        media_type=content_type,
        headers={"Cache-Control": f"public, max-age={_CACHE_MAX_AGE_SECONDS}"},
    )
