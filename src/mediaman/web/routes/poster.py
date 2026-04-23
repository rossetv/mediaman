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
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from mediaman.auth.middleware import get_optional_admin
from mediaman.crypto import (
    decrypt_value,
    sign_poster_url,  # noqa: F401 — re-exported for web/templates + tests
    validate_poster_token,
)
from mediaman.db import get_db
from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.download_format import extract_poster_url
from mediaman.services.http_client import SafeHTTPClient, SafeHTTPError
from mediaman.services.url_safety import is_safe_outbound_url

# Remote poster fetches get a tight 3 s read timeout and 4 MiB cap — a
# poster that doesn't download in 3 s is broken, and real posters run
# well under 1 MiB. Keeping this at module scope shares the pool.
_POSTER_HTTP = SafeHTTPClient(
    default_timeout=(3.0, 3.0),
    default_max_bytes=4 * 1024 * 1024,
)

logger = logging.getLogger("mediaman")

router = APIRouter()

_cache_dir: Path | None = None  # populated on first request from app config

# Cache posters for 7 days (response header) — browser won't re-request
_CACHE_MAX_AGE = 7 * 24 * 60 * 60

# Only these mime types are ever served back to the client. Everything
# else is normalised down to image/jpeg so a malicious CDN cannot
# serve ``Content-Type: text/html`` through the proxy and land a
# stored-XSS-via-poster primitive.
_ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# SSRF allow-list for Radarr/Sonarr remote poster fetches.
#
# Exact hostname → permitted ports. Subdomain wildcards are intentionally
# absent: a DNS-rebind attack on e.g. ``evil.image.tmdb.org`` would pass a
# suffix check but fails an exact-match check. Only HTTPS (443) is
# permitted; port 80 or any non-standard port is refused.
_POSTER_ALLOWED_HOSTS: dict[str, tuple[int, ...]] = {
    "image.tmdb.org": (443,),
    "m.media-amazon.com": (443,),
    "images.amazon.com": (443,),
}


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

    Performs exact hostname matching against ``_POSTER_ALLOWED_HOSTS`` —
    no subdomain wildcards — so a DNS-rebind via ``evil.image.tmdb.org``
    cannot bypass the check. Additionally enforces that the port is in the
    permitted set (443 only) and delegates a full DNS-resolution + public-IP
    check to ``is_safe_outbound_url`` with strict egress enabled, catching
    rebind attacks that return a private IP at request time.
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

    # Exact hostname match — no wildcards.
    if host not in _POSTER_ALLOWED_HOSTS:
        return False

    # Port must be in the permitted set. ``parsed.port`` is None when the
    # URL omits the port, which for HTTPS means 443 implicitly.
    port = parsed.port if parsed.port is not None else 443
    if port not in _POSTER_ALLOWED_HOSTS[host]:
        return False

    # Resolve DNS and confirm every returned IP is public. This catches
    # rebind attacks where the initial check passes but the resolver
    # subsequently returns a private address.
    return is_safe_outbound_url(url, strict_egress=True)


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


def _fetch_arr_poster(conn, rating_key: str, plex_token_row, config) -> tuple[bytes | None, str | None]:
    """Try to fetch a poster from Radarr/Sonarr TMDB data for a media item.

    Looks up the stored ``radarr_id`` / ``sonarr_id`` on the
    ``media_items`` row for this Plex rating key and fetches the
    poster for that exact Arr record. The old implementation matched
    by *title* (case-insensitive substring variants), which let a
    request for a freshly-added "Inception" pull the poster of a
    different Arr row that happened to share the title — a poster
    cache-poisoning primitive (C16).

    Returns (content_bytes, content_type) or (None, None). If no
    ``radarr_id`` / ``sonarr_id`` is populated yet on the stored row
    (common right after an add, before the next scan has backfilled
    IDs), returns (None, None) and the caller will 404 rather than
    guess a replacement.

    *config* is the already-loaded app config object, passed in from the
    request handler to avoid redundant ``load_config()`` calls per
    request (H25).
    """
    row = conn.execute(
        "SELECT title, media_type, radarr_id, sonarr_id "
        "FROM media_items WHERE id = ?",
        (rating_key,),
    ).fetchone()
    if not row:
        return None, None

    title = row["title"]
    media_type = row["media_type"] or "movie"
    radarr_id = row["radarr_id"]
    sonarr_id = row["sonarr_id"]

    poster_url = None

    if media_type == "movie":
        if not radarr_id:
            logger.info(
                "Poster fallback skipped — no radarr_id stored for media id=%s title=%r",
                rating_key, title,
            )
            return None, None
        radarr_client = build_radarr_from_db(conn, config.secret_key)
        if radarr_client:
            try:
                for movie in radarr_client.get_movies():
                    if movie.get("id") == radarr_id:
                        poster_url = extract_poster_url(movie.get("images"))
                        break
            except Exception:
                logger.warning("Failed to fetch Radarr poster for id=%s", radarr_id, exc_info=True)
    else:
        if not sonarr_id:
            logger.info(
                "Poster fallback skipped — no sonarr_id stored for media id=%s title=%r",
                rating_key, title,
            )
            return None, None
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client:
            try:
                for series in sonarr_client.get_series():
                    if series.get("id") == sonarr_id:
                        poster_url = extract_poster_url(series.get("images"))
                        break
            except Exception:
                logger.warning("Failed to fetch Sonarr poster for id=%s", sonarr_id, exc_info=True)

    if not poster_url:
        return None, None

    if not _is_allowed_poster_host(poster_url):
        logger.warning(
            "Refusing Radarr/Sonarr poster fetch for disallowed host: %s",
            poster_url,
        )
        return None, None

    try:
        resp = _POSTER_HTTP.get(poster_url)
        return resp.content, _safe_mime(resp.headers.get("Content-Type"))
    except SafeHTTPError as exc:
        logger.warning(
            "Arr poster fetch refused/failed: %s (%s)",
            poster_url, exc.status_code,
        )
    except Exception:
        logger.warning("Failed to fetch arr poster from %s", poster_url, exc_info=True)

    return None, None


def _sanitise_plex_url(raw: str | None) -> str | None:
    """Return ``scheme://host[:port]`` if *raw* passes SSRF + scheme checks.

    This runs on every poster request. The DB-stored ``plex_url`` could
    have been rotated by an attacker who lands settings-write since the
    app last started; a one-shot startup validation is not enough.
    Userinfo (``user:pass@``), non-http(s) schemes, and anything the
    SSRF guard refuses all result in ``None``.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = urlparse(raw.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if "@" in (parsed.netloc or ""):
        return None
    if not parsed.hostname:
        return None
    # Run the central SSRF check before we use the URL. This re-resolves
    # DNS, so a rebind answer would be caught here.
    if not is_safe_outbound_url(raw):
        return None
    authority = parsed.hostname
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{parsed.scheme.lower()}://{authority}"


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
    config = request.app.state.config
    if admin is None:
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
        plex_token = decrypt_value(
            plex_token, config.secret_key, conn=conn, aad=b"plex_token"
        )

    # Re-validate plex_url on every call — it sits in the DB for weeks
    # and an attacker who lands a settings write could have swapped it
    # for something hostile (cloud metadata, loopback). Reject URLs
    # with userinfo, non-http(s) schemes, and anything that fails the
    # general SSRF guard; then strip back to scheme://host[:port]/ so
    # path-traversal smuggling via the stored URL cannot reach a
    # different endpoint than the templated thumb URL we expect.
    plex_base = _sanitise_plex_url(plex_url)
    if plex_base is None:
        logger.warning(
            "Refusing Plex poster fetch — plex_url failed per-request safety check"
        )
        return Response(status_code=502)

    thumb_url = f"{plex_base}/library/metadata/{rating_key}/thumb"
    content = None
    content_type = "image/jpeg"
    try:
        resp = _POSTER_HTTP.get(
            thumb_url,
            headers={"X-Plex-Token": plex_token},
        )
        content = resp.content
        content_type = _safe_mime(resp.headers.get("Content-Type"))
    except SafeHTTPError as exc:
        logger.warning(
            "Plex poster fetch failed for rating_key=%s (%s)",
            rating_key, exc.status_code,
        )
    except Exception:
        logger.warning("Failed to fetch Plex poster for rating_key=%s", rating_key, exc_info=True)

    # Fallback: fetch poster from Radarr/Sonarr via TMDB if Plex has none
    if content is None:
        content, content_type = _fetch_arr_poster(conn, rating_key, plex_token_row, config)
        if content is None:
            logger.info("Poster unavailable for rating_key=%s — returning 404", rating_key)
            return Response(status_code=404)

    # Write to cache atomically: write to a temp file in the same directory
    # (guaranteeing same filesystem), then os.replace() it into place.
    # This prevents another reader from seeing a partial write.
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_dir, delete=False, suffix=".tmp"
        ) as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        os.replace(tmp_name, cached_path)
    except OSError:
        logger.warning("Poster cache write failed for %s", rating_key, exc_info=True)

    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": f"public, max-age={_CACHE_MAX_AGE}"},
    )
