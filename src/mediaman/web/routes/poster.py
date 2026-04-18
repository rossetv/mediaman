"""Proxy Plex poster images with on-disk caching.

Keeps the Plex token out of the frontend. Posters are cached to
``MEDIAMAN_DATA_DIR/poster_cache/`` on first fetch and served from
disk on subsequent requests, avoiding repeated round-trips to Plex.

Access control
--------------

Logged-in admins can fetch any poster by rating key. Email clients have
no session cookie, so email-embedded posters must be rendered with a
signed URL produced by :func:`sign_poster_url`, which attaches an
HMAC-SHA256 signature of the rating key under ``?sig=...``. The signed
variant is the only way unauthenticated callers can hit this endpoint.
"""

import base64
import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import urlparse

import requests as http_requests
from fastapi import APIRouter, Depends
from fastapi.responses import Response

from mediaman.auth.middleware import get_optional_admin
from mediaman.crypto import decrypt_value
from mediaman.db import get_db

router = APIRouter()

_cache_dir: Path | None = None

# Cache posters for 7 days (response header) — browser won't re-request
_CACHE_MAX_AGE = 7 * 24 * 60 * 60

# SSRF allow-list for Radarr/Sonarr remote poster fetches — only trust
# known image CDNs. Any host outside this list is refused.
_POSTER_ALLOWED_HOST_SUFFIXES = (
    "tmdb.org",
    "themoviedb.org",
    "imdb.com",
)


def sign_poster_url(rating_key: str, secret_key: str) -> str:
    """Return a signed ``/api/poster/{rating_key}?sig=...`` URL.

    The signature is a URL-safe base64 encoding of the HMAC-SHA256 of
    the *rating_key* bytes using *secret_key*. Used by the newsletter
    service so email clients (which have no session cookie) can still
    fetch posters from the authenticated proxy endpoint.
    """
    sig = hmac.new(
        secret_key.encode(), rating_key.encode(), hashlib.sha256
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"/api/poster/{rating_key}?sig={sig_b64}"


def _verify_poster_signature(rating_key: str, sig: str, secret_key: str) -> bool:
    """Verify an HMAC signature attached to a poster URL.

    Accepts base64-urlsafe-encoded signatures with optional trailing
    ``=`` padding. Uses :func:`hmac.compare_digest` for constant-time
    comparison so a tampered signature cannot be brute-forced via
    timing.
    """
    if not sig:
        return False
    try:
        padded = sig + "=" * (-len(sig) % 4)
        provided = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return False
    expected = hmac.new(
        secret_key.encode(), rating_key.encode(), hashlib.sha256
    ).digest()
    return hmac.compare_digest(provided, expected)


def _get_cache_dir() -> Path:
    """Return (and lazily create) the poster cache directory."""
    global _cache_dir
    if _cache_dir is None:
        data_dir = os.environ.get("MEDIAMAN_DATA_DIR", "/data")
        _cache_dir = Path(data_dir) / "poster_cache"
        _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def _is_allowed_poster_host(url: str) -> bool:
    """Return True only for HTTPS URLs pointing at a trusted image CDN.

    Accepts any subdomain of the allow-listed hosts (tmdb.org,
    themoviedb.org, imdb.com). Anything else — including HTTP, IP
    literals, or unknown hosts — is rejected to prevent SSRF via
    attacker-controlled Radarr/Sonarr ``remoteUrl`` values.
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


def _fetch_arr_poster(conn, rating_key: str, plex_token_row) -> tuple:
    """Try to fetch a poster from Radarr/Sonarr TMDB data for a media item.

    Looks up the title from media_items by rating_key, then searches
    Radarr (movies) and Sonarr (series) for a TMDB poster URL.

    Returns (content_bytes, content_type) or (None, None).
    """
    import json
    import logging
    from mediaman.config import load_config

    logger = logging.getLogger("mediaman")

    row = conn.execute(
        "SELECT title, media_type FROM media_items WHERE id = ?",
        (rating_key,),
    ).fetchone()
    if not row:
        return None, None

    config = load_config()
    title = row["title"]
    media_type = row["media_type"] or "movie"

    def _setting(key):
        r = conn.execute("SELECT value, encrypted FROM settings WHERE key=?", (key,)).fetchone()
        if not r or not r["value"]:
            return ""
        val = r["value"]
        if r["encrypted"]:
            try:
                val = decrypt_value(val, config.secret_key, conn=conn)
            except Exception:
                return ""
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val or ""

    poster_url = None

    if media_type == "movie":
        radarr_url = _setting("radarr_url")
        radarr_key = _setting("radarr_api_key")
        if radarr_url and radarr_key:
            try:
                from mediaman.services.radarr import RadarrClient
                client = RadarrClient(radarr_url, radarr_key)
                for movie in client.get_movies():
                    if movie.get("title", "").lower() == title.lower():
                        for img in movie.get("images") or []:
                            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                                poster_url = img["remoteUrl"]
                                break
                        break
            except Exception:
                pass
    else:
        sonarr_url = _setting("sonarr_url")
        sonarr_key = _setting("sonarr_api_key")
        if sonarr_url and sonarr_key:
            try:
                from mediaman.services.sonarr import SonarrClient
                client = SonarrClient(sonarr_url, sonarr_key)
                for series in client.get_series():
                    if series.get("title", "").lower() == title.lower():
                        for img in series.get("images") or []:
                            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                                poster_url = img["remoteUrl"]
                                break
                        break
            except Exception:
                pass

    if not poster_url:
        return None, None

    if not _is_allowed_poster_host(poster_url):
        logger.warning(
            "Refusing Radarr/Sonarr poster fetch for disallowed host: %s",
            poster_url,
        )
        return None, None

    try:
        resp = http_requests.get(poster_url, timeout=10, allow_redirects=False)
        if resp.status_code == 200:
            return resp.content, resp.headers.get("Content-Type", "image/jpeg")
    except Exception:
        pass

    return None, None


def _validate_rating_key(rating_key: str) -> bool:
    """Return True if rating_key is a valid Plex rating key (digits only)."""
    return bool(rating_key) and rating_key.isdigit()


@router.get("/api/poster/{rating_key}")
def proxy_poster(
    rating_key: str,
    sig: str | None = None,
    admin: str | None = Depends(get_optional_admin),
):
    """Serve a poster image, fetching from Plex only on cache miss.

    Authentication: either an active admin session, or a valid HMAC
    signature in ``?sig=...`` (generated via :func:`sign_poster_url`).
    Unauthenticated callers with no or bad signature get a 401.
    """
    if not _validate_rating_key(rating_key):
        return Response(status_code=404)

    if admin is None:
        from mediaman.config import load_config
        config = load_config()
        if not _verify_poster_signature(rating_key, sig or "", config.secret_key):
            return Response(status_code=401)

    cache_dir = _get_cache_dir()
    # Use a safe filename derived from the rating key
    safe_name = hashlib.sha256(rating_key.encode()).hexdigest()[:16]
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
        plex_token = decrypt_value(plex_token, config.secret_key, conn=conn)

    thumb_url = f"{plex_url}/library/metadata/{rating_key}/thumb"
    content = None
    content_type = "image/jpeg"
    try:
        # allow_redirects=False so the X-Plex-Token header can't leak to a
        # third-party host if Plex (or a MITM) responds with a redirect.
        resp = http_requests.get(
            thumb_url, timeout=10,
            headers={"X-Plex-Token": plex_token},
            allow_redirects=False,
        )
        if resp.status_code == 200:
            content = resp.content
            content_type = resp.headers.get("Content-Type", "image/jpeg")
    except Exception:
        pass

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
