"""Proxy Plex poster images with on-disk caching.

Keeps the Plex token out of the frontend. Posters are cached to
``MEDIAMAN_DATA_DIR/poster_cache/`` on first fetch and served from
disk on subsequent requests, avoiding repeated round-trips to Plex.

Access control
--------------

Logged-in admins can fetch any poster by rating key. Email clients have
no session cookie, so email-embedded posters must be rendered with a
signed URL produced by :func:`sign_poster_url`. The signature now
carries an expiry (default 180 days) and is domain-separated via a
dedicated HMAC sub-key (see :mod:`mediaman.crypto`) so a stolen
poster URL cannot outlive its legitimate newsletter forever and
cannot be confused with other token types.

Unauthenticated callers (no session, no valid signed token) receive
a uniform 401 regardless of whether the rating_key exists on the
Plex server. This prevents the endpoint being used as an existence
oracle to enumerate the user's library rating keys.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse

import requests as http_requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from mediaman.auth.middleware import get_optional_admin
from mediaman.crypto import (
    decrypt_value,
    generate_poster_token,
    validate_poster_token,
)
from mediaman.db import get_db
from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.download_format import extract_poster_url

logger = logging.getLogger("mediaman")

router = APIRouter()

_cache_dir: Path | None = None  # populated on first request from app config

# Cache posters for 7 days (response header) — browser won't re-request
_CACHE_MAX_AGE = 7 * 24 * 60 * 60

# Hard cap on the size of a remote-fetched poster. Real posters run
# 100-800 KB; 10 MB is generous and stops a compromised CDN streaming
# an unbounded body into the cache.
_MAX_POSTER_BYTES = 10 * 1024 * 1024

# Only these mime types are ever served back to the client. Everything
# else is normalised down to image/jpeg so a malicious CDN cannot
# serve ``Content-Type: text/html`` through the proxy and land a
# stored-XSS-via-poster primitive.
_ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# SSRF allow-list for Radarr/Sonarr remote poster fetches — only trust
# known image CDNs. Any host outside this list is refused.
_POSTER_ALLOWED_HOST_SUFFIXES = (
    "tmdb.org",
    "themoviedb.org",
    "imdb.com",
    "media-amazon.com",
)


def sign_poster_url(rating_key: str, secret_key: str) -> str:
    """Return a signed ``/api/poster/{rating_key}?sig=...`` URL.

    Uses the new expiry-bearing poster token so emails eventually
    stop working as access credentials. Used by the newsletter
    service so email clients (which have no session cookie) can
    still fetch posters from the authenticated proxy endpoint.
    """
    token = generate_poster_token(rating_key, secret_key)
    return f"/api/poster/{rating_key}?sig={token}"


def _get_cache_dir(data_dir: str) -> Path:
    """Return (and lazily create) the poster cache directory.

    The lifespan in ``main.py`` calls this at startup so the directory
    exists before any request arrives. The lazy-init guard keeps subsequent
    per-request calls cheap (a module-level attribute check rather than a
    filesystem stat) while remaining safe if called before startup completes
    in tests or CLI contexts.
    """
    global _cache_dir
    if _cache_dir is None:
        _cache_dir = Path(data_dir) / "poster_cache"
        _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def _is_allowed_poster_host(url: str) -> bool:
    """Return True only for HTTPS URLs pointing at a trusted image CDN.

    Accepts any subdomain of the allow-listed hosts (tmdb.org,
    themoviedb.org, imdb.com, media-amazon.com). Anything else —
    including HTTP, IP literals, or unknown hosts — is rejected to
    prevent SSRF via attacker-controlled Radarr/Sonarr ``remoteUrl``
    values.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _POSTER_ALLOWED_HOST_SUFFIXES
    )


def _safe_mime(remote_type: str | None) -> str:
    """Coerce a remote ``Content-Type`` into a safe served mime.

    If the remote response claims a type we know is image-shaped, pass
    it through. Otherwise default to ``image/jpeg`` — the caller only
    ever sets this for payloads we fetched from a trusted image CDN
    or the Plex thumb endpoint, so type confusion is not a real risk
    but XSS-via-content-type absolutely is.
    """
    if not remote_type:
        return "image/jpeg"
    base = remote_type.split(";", 1)[0].strip().lower()
    if base in _ALLOWED_IMAGE_MIMES:
        return base
    return "image/jpeg"


def _stream_capped(response) -> bytes | None:
    """Read up to ``_MAX_POSTER_BYTES`` from *response* and return bytes.

    Returns None if the server advertised an oversize body via
    Content-Length OR if the streamed body exceeds the cap mid-read.
    """
    cl = response.headers.get("Content-Length")
    if cl:
        try:
            if int(cl) > _MAX_POSTER_BYTES:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_POSTER_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_arr_poster(conn, rating_key: str, plex_token_row) -> tuple:
    """Try to fetch a poster from Radarr/Sonarr TMDB data for a media item.

    Looks up the title from media_items by rating_key, then searches
    Radarr (movies) and Sonarr (series) for a TMDB poster URL.

    Returns (content_bytes, content_type) or (None, None).
    """
    from mediaman.config import load_config

    row = conn.execute(
        "SELECT title, media_type FROM media_items WHERE id = ?",
        (rating_key,),
    ).fetchone()
    if not row:
        return None, None

    title = row["title"]
    media_type = row["media_type"] or "movie"

    poster_url = None

    if media_type == "movie":
        config = load_config()
        radarr_client = build_radarr_from_db(conn, config.secret_key)
        if radarr_client:
            try:
                for movie in radarr_client.get_movies():
                    if movie.get("title", "").lower() == title.lower():
                        poster_url = extract_poster_url(movie.get("images")) or ""
                        break
            except Exception:
                logger.warning("Failed to fetch Radarr poster for title=%r", title, exc_info=True)
    else:
        config = load_config()
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client:
            try:
                for series in sonarr_client.get_series():
                    if series.get("title", "").lower() == title.lower():
                        poster_url = extract_poster_url(series.get("images")) or ""
                        break
            except Exception:
                logger.warning("Failed to fetch Sonarr poster for title=%r", title, exc_info=True)

    if not poster_url:
        return None, None

    if not _is_allowed_poster_host(poster_url):
        logger.warning(
            "Refusing Radarr/Sonarr poster fetch for disallowed host: %s",
            poster_url,
        )
        return None, None

    try:
        resp = http_requests.get(
            poster_url, timeout=10, allow_redirects=False, stream=True,
        )
        if resp.status_code == 200:
            body = _stream_capped(resp)
            if body is None:
                logger.warning("Poster body exceeded size cap from %s", poster_url)
                return None, None
            return body, _safe_mime(resp.headers.get("Content-Type"))
    except Exception:
        logger.warning("Failed to fetch arr poster from %s", poster_url, exc_info=True)

    return None, None


def _validate_rating_key(rating_key: str) -> bool:
    """Return True if rating_key is a valid Plex rating key (digits only)."""
    return bool(rating_key) and rating_key.isdigit() and len(rating_key) <= 12


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
    """
    # Auth FIRST — return 401 for any unauthenticated caller regardless
    # of whether the rating_key is well-formed or present on the Plex
    # server. This closes the enumeration oracle noted in the pentest.
    if admin is None:
        from mediaman.config import load_config
        config = load_config()
        if not sig or len(sig) > 4096:
            return Response(status_code=401)
        if not _validate_rating_key(rating_key):
            return Response(status_code=401)
        if not validate_poster_token(sig, config.secret_key, rating_key):
            return Response(status_code=401)
    else:
        if not _validate_rating_key(rating_key):
            return Response(status_code=404)

    cache_dir = _get_cache_dir(request.app.state.config.data_dir)
    # Use a safe filename derived from the rating key. Full SHA-256 so
    # we're never worried about collisions; filesystems handle 64 hex
    # chars without complaint.
    safe_name = hashlib.sha256(rating_key.encode()).hexdigest()
    cached_path = cache_dir / f"{safe_name}.jpg"

    # Serve from cache if available
    if cached_path.exists():
        return Response(
            content=cached_path.read_bytes(),
            media_type="image/jpeg",
            headers={"Cache-Control": f"public, max-age={_CACHE_MAX_AGE}"},
        )

    # Cache miss — fetch from Plex
    conn = get_db()

    plex_url_row = conn.execute(
        "SELECT value FROM settings WHERE key='plex_url'"
    ).fetchone()
    plex_token_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='plex_token'"
    ).fetchone()

    if not plex_url_row or not plex_token_row:
        return Response(status_code=404)

    plex_url = plex_url_row["value"]
    plex_token = plex_token_row["value"]
    if plex_token_row["encrypted"]:
        from mediaman.config import load_config
        config = load_config()
        plex_token = decrypt_value(
            plex_token, config.secret_key, conn=conn, aad=b"plex_token"
        )

    thumb_url = f"{plex_url}/library/metadata/{rating_key}/thumb"
    content = None
    content_type = "image/jpeg"
    try:
        # allow_redirects=False so the X-Plex-Token header can't leak to a
        # third-party host if Plex (or a MITM) responds with a redirect.
        # stream=True + _stream_capped caps body size at 10 MiB.
        resp = http_requests.get(
            thumb_url, timeout=10,
            headers={"X-Plex-Token": plex_token},
            allow_redirects=False,
            stream=True,
        )
        if resp.status_code == 200:
            body = _stream_capped(resp)
            if body is not None:
                content = body
                content_type = _safe_mime(resp.headers.get("Content-Type"))
    except Exception:
        logger.warning("Failed to fetch Plex poster for rating_key=%s", rating_key, exc_info=True)

    # Fallback: fetch poster from Radarr/Sonarr via TMDB if Plex has none
    if content is None:
        content, content_type = _fetch_arr_poster(conn, rating_key, plex_token_row)
        if content is None:
            return Response(status_code=404)

    # Write to cache (atomic via temp file to avoid serving partial writes)
    tmp_path = cached_path.with_suffix(".tmp")
    try:
        tmp_path.write_bytes(content)
        tmp_path.rename(cached_path)
    except OSError:
        pass  # Cache write failure is non-fatal

    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": f"public, max-age={_CACHE_MAX_AGE}"},
    )
