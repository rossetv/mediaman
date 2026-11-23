"""Proxy Plex poster images with on-disk caching, keeping the Plex token out of the frontend.

Package layout: cache.py holds the filesystem cache, fetch.py the outbound fetch logic, SSRF
guards and URL/mime helpers, and this module the route handler, rate limiters, auth gate and
re-exports of the names tests patch at mediaman.web.routes.poster.<name>.

Access control: anyone logged in, admin or viewer, can fetch the poster of an item in the
scanned libraries (or of its show, or of an item recorded as deleted); any other key is a 404. Email clients have no
session cookie, so email-embedded posters use a signed URL from sign_poster_url, whose
signature carries an expiry (default 180 days) and is domain-separated via a dedicated HMAC
sub-key. Unauthenticated callers get a uniform 401 whether or not the rating_key exists, so
the endpoint cannot serve as an existence oracle for the library.
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
from mediaman.web.repository.watch_library import is_poster_key_allowed, scanned_library_ids
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

# These names are imported for test patching purposes only — tests that
# need to mock poster-fetch internals patch them via the absolute path
# ``mediaman.web.routes.poster.<name>``.  They are not re-exported in
# ``__all__`` (private names must not be in a public surface) but must
# be importable from this module for the test suite to patch them.
from mediaman.web.routes.poster.fetch import _POSTER_HTTP  # noqa: F401
from mediaman.web.routes.poster.fetch import fetch_arr_poster as _fetch_arr_poster  # noqa: F401
from mediaman.web.routes.poster.fetch import (
    is_allowed_poster_host as _is_allowed_poster_host,  # noqa: F401
)
from mediaman.web.routes.poster.fetch import (
    is_valid_rating_key as _validate_rating_key,
)
from mediaman.web.routes.poster.fetch import load_plex_credentials as _load_plex_credentials
from mediaman.web.routes.poster.fetch import resolve_poster_content as _resolve_poster_content
from mediaman.web.routes.poster.fetch import safe_mime as _safe_mime  # noqa: F401

__all__ = [
    # Public names this barrel re-exports for callers.
    # Private names (_POSTER_HTTP, _fetch_arr_poster, etc.) are patched by
    # tests via their absolute module paths, so they stay out of __all__.
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
    """Authenticate a poster request; return an error Response, or None on success.

    Auth comes first: any unauthenticated caller gets 401 whether or not the rating_key is
    well-formed or exists, which closes the enumeration oracle. Admin sessions skip the IP
    cap since the auth layer already bounds them.
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

    Needs an admin session or a valid HMAC signature in ?sig=. The 401 comes before any
    rating_key validity or existence check, so the endpoint is no oracle for enumerating
    rating keys. Unauthenticated (signed-URL) callers are also rate-limited per /24 (IPv4)
    or /64 (IPv6) at 60 req/min so a leaked URL cannot amplify bandwidth.
    """
    config = request.app.state.config
    auth_err = _authenticate_poster_request(request, rating_key, sig, admin, config.secret_key)
    if auth_err is not None:
        return auth_err

    # A signed URL was issued by mediaman for that key; a session, admin or viewer, may only
    # reach posters of items in the scanned libraries (or recorded as deleted).
    if admin is not None:
        db = get_db()
        if not is_poster_key_allowed(db, scanned_library_ids(db), rating_key):
            return Response(status_code=404)

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
