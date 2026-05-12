"""Outbound poster fetch logic and pure URL/mime helpers.

These helpers handle SSRF defence, mime coercion, and the actual HTTP
calls into Plex and Radarr/Sonarr.  They are kept in their own module
so they can be tested in isolation and so :mod:`__init__` stays focused
on FastAPI route handlers.

Threat model
------------
The primary concern for poster fetching is SSRF (Server-Side Request
Forgery).  An attacker who can influence the Plex URL stored in the
database — or who can inject a Radarr/Sonarr poster URL — could redirect
the proxy to an internal metadata endpoint, loopback address, or cloud
credential service.  The helpers here form the first line of defence:

* :func:`is_valid_rating_key` rejects non-numeric and oversized keys
  before they can reach any URL construction or DB lookup.
* :func:`safe_mime` prevents a hostile CDN from injecting
  ``Content-Type: text/html`` through the proxy — a stored-XSS vector.
* :func:`is_allowed_poster_host` performs exact hostname matching plus
  a DNS-resolved public-IP check via
  :func:`mediaman.services.infra.url_safety.is_safe_outbound_url`.
* :func:`sanitise_plex_url` re-validates the DB-stored ``plex_url`` on
  every request because a settings-write compromise could otherwise
  swap it for a hostile target.
"""

from __future__ import annotations

import logging
import sqlite3
from urllib.parse import urlparse

import requests
from fastapi.responses import Response

from mediaman.config import Config
from mediaman.crypto import decrypt_value
from mediaman.services.arr import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.infra.http import SafeHTTPClient, SafeHTTPError
from mediaman.services.infra.url_safety import is_safe_outbound_url
from mediaman.web.repository.poster import PosterArrIds, fetch_arr_ids, fetch_plex_credentials
from mediaman.web.routes.poster.cache import ALLOWED_IMAGE_MIMES

logger = logging.getLogger(__name__)

# Remote poster fetches get a tight 3 s read timeout and 4 MiB cap — a
# poster that doesn't download in 3 s is broken, and real posters run
# well under 1 MiB. Keeping this at module scope shares the pool.
_POSTER_HTTP = SafeHTTPClient(
    default_timeout=(3.0, 3.0),
    default_max_bytes=4 * 1024 * 1024,
)

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


def is_valid_rating_key(rating_key: str) -> bool:
    """Return ``True`` only if *rating_key* is a valid Plex rating key.

    A valid rating key is a non-empty string of ASCII digits whose total
    length does not exceed 12 characters.  This rejects path-traversal
    sequences (``../``, ``%2F``), alphabetic strings, and arbitrarily
    long keys before they touch any URL template or filesystem path.
    """
    return bool(rating_key) and rating_key.isdigit() and len(rating_key) <= 12


def safe_mime(remote_type: str | None) -> str:
    """Coerce a remote ``Content-Type`` value into a safe served mime type.

    If the upstream response claims a type from :data:`~.cache.ALLOWED_IMAGE_MIMES`,
    it is passed through unchanged.  Everything else — including missing,
    malformed, or hostile values such as ``text/html`` — is normalised to
    ``image/jpeg``.  This is the primary defence against a malicious CDN
    using the poster proxy as a stored-XSS vector.
    """
    if not remote_type:
        return "image/jpeg"
    base = remote_type.split(";", 1)[0].strip().lower()
    if base in ALLOWED_IMAGE_MIMES:
        return base
    return "image/jpeg"


def is_allowed_poster_host(url: str) -> bool:
    """Return ``True`` only for HTTPS URLs pointing at a trusted image CDN.

    Performs exact hostname matching against ``_POSTER_ALLOWED_HOSTS`` —
    no subdomain wildcards — so a DNS-rebind via ``evil.image.tmdb.org``
    cannot bypass the check.  Additionally enforces that the port is in
    the permitted set (443 only) and delegates a full DNS-resolution +
    public-IP check to :func:`is_safe_outbound_url` with strict egress
    enabled, catching rebind attacks that return a private IP at
    request time.
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
    return bool(is_safe_outbound_url(url, strict_egress=True))


def sanitise_plex_url(raw: str | None) -> str | None:
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


def load_plex_credentials(
    conn: sqlite3.Connection, secret_key: str
) -> tuple[str | None, str | None, Response | None]:
    """Load and decrypt the Plex URL and token from the DB.

    Returns ``(plex_base, plex_token, None)`` on success, or
    ``(None, None, error_response)`` when either field is absent or
    the URL fails per-request SSRF re-validation.
    """
    creds = fetch_plex_credentials(conn)
    if creds is None or creds.url is None or creds.token_ciphertext is None:
        return None, None, Response(status_code=404)
    plex_url = creds.url
    plex_token = creds.token_ciphertext
    if creds.token_encrypted:
        plex_token = decrypt_value(plex_token, secret_key, conn=conn, aad=b"plex_token")
    # Re-validate plex_url on every call — it sits in the DB for weeks
    # and an attacker who lands a settings write could have swapped it
    # for something hostile.  Strip back to scheme://host[:port]/ so
    # path-traversal smuggling via the stored URL cannot reach a
    # different endpoint than the templated thumb URL we expect.
    plex_base = sanitise_plex_url(plex_url)
    if plex_base is None:
        logger.warning("Refusing Plex poster fetch — plex_url failed per-request safety check")
        return None, None, Response(status_code=502)
    return plex_base, plex_token, None


def fetch_plex_poster(
    plex_base: str, plex_token: str, rating_key: str, *, http_client: SafeHTTPClient | None = None
) -> tuple[bytes | None, str]:
    """Fetch a poster from Plex.  Returns ``(content, content_type)`` or ``(None, 'image/jpeg')``.

    *http_client* defaults to :data:`_POSTER_HTTP`; tests substitute a stub.
    """
    client = http_client if http_client is not None else _POSTER_HTTP
    thumb_url = f"{plex_base}/library/metadata/{rating_key}/thumb"
    try:
        resp = client.get(thumb_url, headers={"X-Plex-Token": plex_token})
        return resp.content, safe_mime(resp.headers.get("Content-Type"))
    except SafeHTTPError as exc:
        logger.warning(
            "Plex poster fetch failed for rating_key=%s (%s)",
            rating_key,
            exc.status_code,
        )
    except requests.RequestException:
        logger.warning("Failed to fetch Plex poster for rating_key=%s", rating_key, exc_info=True)
    return None, "image/jpeg"


def _resolve_arr_poster_url(
    conn: sqlite3.Connection, row: PosterArrIds, config: Config
) -> tuple[str | None, str | None]:
    """Look up the Radarr/Sonarr poster URL for a stored media row.

    Returns ``(poster_url, title)`` or ``(None, None)`` when the row
    has no Arr ID or the upstream lookup fails.  Network errors are
    swallowed at the warning level — the caller falls back to 404.
    """
    title = row.title
    media_type = row.media_type
    radarr_id = row.radarr_id
    sonarr_id = row.sonarr_id

    if media_type == "movie":
        if not radarr_id:
            logger.info("Poster fallback skipped — no radarr_id stored for media title=%r", title)
            return None, title
        radarr_client = build_radarr_from_db(conn, config.secret_key)
        if not radarr_client:
            return None, title
        try:
            for movie in radarr_client.get_movies():
                if movie.get("id") == radarr_id:
                    return extract_poster_url(movie.get("images")), title
        except (requests.RequestException, SafeHTTPError, ArrError):
            logger.warning("Failed to fetch Radarr poster for id=%s", radarr_id, exc_info=True)
        return None, title

    if not sonarr_id:
        logger.info("Poster fallback skipped — no sonarr_id stored for media title=%r", title)
        return None, title
    sonarr_client = build_sonarr_from_db(conn, config.secret_key)
    if not sonarr_client:
        return None, title
    try:
        for series in sonarr_client.get_series():
            if series.get("id") == sonarr_id:
                return extract_poster_url(series.get("images")), title
    except (requests.RequestException, SafeHTTPError, ArrError):
        logger.warning("Failed to fetch Sonarr poster for id=%s", sonarr_id, exc_info=True)
    return None, title


def _fetch_allowed_poster_bytes(poster_url: str) -> tuple[bytes | None, str | None]:
    """Fetch poster bytes from *poster_url* after host-allow-list check.

    Returns ``(content_bytes, content_type)`` or ``(None, None)`` when
    the host is disallowed or the fetch fails.
    """
    if not is_allowed_poster_host(poster_url):
        logger.warning("Refusing Radarr/Sonarr poster fetch for disallowed host: %s", poster_url)
        return None, None
    try:
        resp = _POSTER_HTTP.get(poster_url)
        return resp.content, safe_mime(resp.headers.get("Content-Type"))
    except SafeHTTPError as exc:
        logger.warning("Arr poster fetch refused/failed: %s (%s)", poster_url, exc.status_code)
    except requests.RequestException:
        logger.warning("Failed to fetch arr poster from %s", poster_url, exc_info=True)
    return None, None


def fetch_arr_poster(
    conn: sqlite3.Connection, rating_key: str, config: Config
) -> tuple[bytes | None, str | None]:
    """Try to fetch a poster from Radarr/Sonarr TMDB data for a media item.

    Looks up the stored ``radarr_id`` / ``sonarr_id`` on the
    ``media_items`` row for this Plex rating key and fetches the
    poster for that exact Arr record.  Matching by ID rather than
    title prevents a cache-poisoning primitive (C16) where two media
    items share a title but have different Arr IDs.

    Returns ``(content_bytes, content_type)`` or ``(None, None)`` when
    no source can supply the poster.  The caller responds with 404 in
    that case rather than guess a replacement.
    """
    row = fetch_arr_ids(conn, rating_key)
    if not row:
        return None, None

    poster_url, _title = _resolve_arr_poster_url(conn, row, config)
    if not poster_url:
        return None, None
    return _fetch_allowed_poster_bytes(poster_url)


def resolve_poster_content(
    conn: sqlite3.Connection, rating_key: str, plex_base: str, plex_token: str, config: Config
) -> tuple[bytes | None, str, Response | None]:
    """Fetch poster bytes from Plex with Radarr/Sonarr fallback.

    Returns ``(content, content_type, None)`` on success, or
    ``(None, '', 404_response)`` when no source has a poster.
    """
    content, content_type = fetch_plex_poster(plex_base, plex_token, rating_key)
    # Fallback: fetch poster from Radarr/Sonarr via TMDB if Plex has none
    if content is None:
        content, fallback_type = fetch_arr_poster(conn, rating_key, config)
        if content is None:
            logger.info("Poster unavailable for rating_key=%s — returning 404", rating_key)
            return None, "", Response(status_code=404)
        content_type = fallback_type or "image/jpeg"
    return content, content_type, None
