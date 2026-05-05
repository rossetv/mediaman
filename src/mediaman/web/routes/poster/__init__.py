"""Proxy Plex poster images with on-disk caching.

Keeps the Plex token out of the frontend. Posters are cached to
``MEDIAMAN_DATA_DIR/poster_cache/`` on first fetch and served from
disk on subsequent requests, avoiding repeated round-trips to Plex.

Package layout
--------------
``poster/cache.py``
    Filesystem helpers: path computation, atomic read/write, sidecar mime
    persistence.  No outbound HTTP.

``poster/fetch.py``
    Pure validation and normalisation helpers: rating-key validation,
    mime coercion.  No I/O.

``poster/__init__.py`` (this module)
    All FastAPI route handlers and the outbound HTTP fetch logic (Plex
    thumb + Radarr/Sonarr fallback), plus the LRU cache sweep.  The
    module-level names that tests patch (``_POSTER_HTTP``,
    ``is_safe_outbound_url``, ``build_radarr_from_db``, ``os``) are
    defined here so ``patch("mediaman.web.routes.poster.<name>")`` works
    correctly.

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

import contextlib
import hashlib
import logging
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from mediaman.core.url_safety import is_safe_outbound_url
from mediaman.crypto import (
    decrypt_value,
    sign_poster_url,  # noqa: F401 — re-exported for web/templates + tests
    validate_poster_token,
)
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError
from mediaman.services.infra.rate_limits import (
    POSTER_PUBLIC_LIMITER as _POSTER_PUBLIC_LIMITER,
)
from mediaman.services.rate_limit import get_client_ip
from mediaman.web.auth.middleware import get_optional_admin

# Import pure helpers from submodules.
from mediaman.web.routes.poster.cache import (
    read_sidecar_mime as _read_sidecar_mime,
)
from mediaman.web.routes.poster.cache import (
    write_sidecar_mime as _write_sidecar_mime,
)
from mediaman.web.routes.poster.fetch import (
    safe_mime as _safe_mime_impl,
)
from mediaman.web.routes.poster.fetch import (
    validate_rating_key as _validate_rating_key_impl,
)

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

# Soft cap for the on-disk poster cache. The directory was previously
# unbounded — a long-lived install would let it grow until the data
# volume filled. 500 MiB is generous (a poster averages ~80 KiB, so
# this caps at roughly 6,500 posters) but keeps disk pressure
# predictable. Cleanup is opportunistic: once total size exceeds the
# cap, the route deletes oldest files by mtime until usage is back
# under the cap. The check is throttled so we don't pay the directory
# walk cost on every request.
_CACHE_DIR_MAX_BYTES = 500 * 1024 * 1024
#: One-in-N requests trigger a real LRU sweep. Rest path-checks just
#: see if the in-memory size estimate exceeds the cap.
_CACHE_GC_RECHECK_EVERY = 50

# Sweep state guarded by the lock so two concurrent requests don't both
# walk the directory. Lock is best-effort — if it cannot be acquired
# without blocking, the request skips the GC and serves immediately.
_cache_gc_lock = threading.Lock()
_cache_gc_counter = 0

# Only these mime types are ever served back to the client.
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

    State is managed via the module-level ``_cache_dir`` attribute so that
    test fixtures can reset it between runs via
    ``poster_mod._cache_dir = None``.
    """
    global _cache_dir
    if _cache_dir is None:
        _cache_dir = Path(data_dir) / "poster_cache"
        _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def _maybe_sweep_cache(cache_dir: Path) -> None:
    """Opportunistically delete oldest cache entries when over the cap.

    Called from the cache-write path. The sweep walks the directory
    once, sums up sizes, and — if total exceeds
    :data:`_CACHE_DIR_MAX_BYTES` — deletes oldest-mtime files until the
    total is back under 90% of the cap (a small headroom so we don't
    sweep on every subsequent write).

    The walk is guarded by a non-blocking lock so concurrent writers
    don't stampede the same directory; if another thread is already
    sweeping, we skip and return. The throttled counter means the
    expensive walk only runs once per ~50 cache misses.
    """
    global _cache_gc_counter
    _cache_gc_counter += 1
    if _cache_gc_counter < _CACHE_GC_RECHECK_EVERY:
        return
    if not _cache_gc_lock.acquire(blocking=False):
        return
    _cache_gc_counter = 0
    try:
        entries: list[tuple[float, int, Path]] = []
        total = 0
        try:
            for entry in cache_dir.iterdir():
                # ``.tmp`` files are short-lived from in-flight writes —
                # don't touch them here. Sidecars are tiny and walk-
                # cheap; sweep them alongside their image so we don't
                # leak orphans.
                try:
                    st = entry.stat()
                except OSError:
                    continue
                size = int(st.st_size)
                total += size
                entries.append((st.st_mtime, size, entry))
        except OSError:
            return

        if total <= _CACHE_DIR_MAX_BYTES:
            return

        # Delete oldest first until 90% of cap.
        entries.sort(key=lambda e: e[0])
        target = int(_CACHE_DIR_MAX_BYTES * 0.9)
        for _mtime, size, path in entries:
            if total <= target:
                break
            try:
                path.unlink()
                total -= size
                # If we removed an image, also unlink its sidecar.
                if path.suffix == ".jpg":
                    sidecar = path.with_suffix(path.suffix + ".mime")
                    try:
                        sidecar.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.debug("Failed to unlink sidecar for %s", path, exc_info=True)
            except FileNotFoundError:
                continue
            except OSError:
                logger.debug("Failed to unlink %s during sweep", path, exc_info=True)
    finally:
        _cache_gc_lock.release()


def _is_allowed_poster_host(url: str) -> bool:
    """Return ``True`` only for HTTPS URLs pointing at a trusted image CDN.

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

    Delegates to :func:`~.fetch.safe_mime`.  Kept as a module-level name
    here so that ``patch("mediaman.web.routes.poster._safe_mime", ...)``
    works correctly in tests.
    """
    return _safe_mime_impl(remote_type)


def _validate_rating_key(rating_key: str) -> bool:
    """Return ``True`` if rating_key is a valid Plex rating key (digits only).

    Delegates to :func:`~.fetch.validate_rating_key`.  Kept as a module-level
    name here so that imports and patches against ``mediaman.web.routes.poster``
    resolve correctly.
    """
    return _validate_rating_key_impl(rating_key)


def _fetch_arr_poster(
    conn, rating_key: str, plex_token_row, config
) -> tuple[bytes | None, str | None]:
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
        "SELECT title, media_type, radarr_id, sonarr_id FROM media_items WHERE id = ?",
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
                rating_key,
                title,
            )
            return None, None
        radarr_client = build_radarr_from_db(conn, config.secret_key)
        if radarr_client:
            try:
                for movie in radarr_client.get_movies():
                    if movie.get("id") == radarr_id:
                        poster_url = extract_poster_url(movie.get("images"))
                        break
            except (requests.RequestException, SafeHTTPError):
                logger.warning("Failed to fetch Radarr poster for id=%s", radarr_id, exc_info=True)
    else:
        if not sonarr_id:
            logger.info(
                "Poster fallback skipped — no sonarr_id stored for media id=%s title=%r",
                rating_key,
                title,
            )
            return None, None
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client:
            try:
                for series in sonarr_client.get_series():
                    if series.get("id") == sonarr_id:
                        poster_url = extract_poster_url(series.get("images"))
                        break
            except (requests.RequestException, SafeHTTPError):
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
            poster_url,
            exc.status_code,
        )
    except requests.RequestException:
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
    # Auth FIRST — return 401 for any unauthenticated caller regardless
    # of whether the rating_key is well-formed or present on the Plex
    # server. This closes the enumeration oracle noted in the pentest.
    config = request.app.state.config
    if admin is None:
        if not _POSTER_PUBLIC_LIMITER.check(get_client_ip(request)):
            return Response(status_code=429)
        if not sig or len(sig) > 4096:
            return Response(status_code=401)
        if not _validate_rating_key(rating_key):
            return Response(status_code=401)
        poster_payload = validate_poster_token(sig, config.secret_key)
        if poster_payload is None or poster_payload.get("rk") != rating_key:
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

    # Serve from cache if available — read mime from sidecar so PNG /
    # WebP entries are served with their actual type and don't trip the
    # nosniff guard on modern browsers.
    if cached_path.exists():
        return Response(
            content=cached_path.read_bytes(),
            media_type=_read_sidecar_mime(cached_path),
            headers={"Cache-Control": f"public, max-age={_CACHE_MAX_AGE}"},
        )

    # Cache miss — fetch from Plex
    conn = get_db()

    plex_url_row = conn.execute("SELECT value FROM settings WHERE key='plex_url'").fetchone()
    plex_token_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='plex_token'"
    ).fetchone()

    if not plex_url_row or not plex_token_row:
        return Response(status_code=404)

    plex_url = plex_url_row["value"]
    plex_token = plex_token_row["value"]
    if plex_token_row["encrypted"]:
        plex_token = decrypt_value(plex_token, config.secret_key, conn=conn, aad=b"plex_token")

    # Re-validate plex_url on every call — it sits in the DB for weeks
    # and an attacker who lands a settings write could have swapped it
    # for something hostile (cloud metadata, loopback). Reject URLs
    # with userinfo, non-http(s) schemes, and anything that fails the
    # general SSRF guard; then strip back to scheme://host[:port]/ so
    # path-traversal smuggling via the stored URL cannot reach a
    # different endpoint than the templated thumb URL we expect.
    plex_base = _sanitise_plex_url(plex_url)
    if plex_base is None:
        logger.warning("Refusing Plex poster fetch — plex_url failed per-request safety check")
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
            rating_key,
            exc.status_code,
        )
    except requests.RequestException:
        logger.warning("Failed to fetch Plex poster for rating_key=%s", rating_key, exc_info=True)

    # Fallback: fetch poster from Radarr/Sonarr via TMDB if Plex has none
    if content is None:
        content, fallback_type = _fetch_arr_poster(conn, rating_key, plex_token_row, config)
        if content is None:
            logger.info("Poster unavailable for rating_key=%s — returning 404", rating_key)
            return Response(status_code=404)
        content_type = fallback_type or "image/jpeg"

    # Write to cache atomically: write to a temp file in the same directory
    # (guaranteeing same filesystem), then os.replace() it into place.
    # This prevents another reader from seeing a partial write. On
    # failure after the temp file was written, the temp must be removed
    # explicitly — leaving it would orphan disk space until the next
    # sweep.
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False, suffix=".tmp") as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        os.replace(tmp_name, cached_path)
        tmp_name = None  # Success — replace consumed the temp.
        # Persist the served mime so subsequent cache hits return the
        # same Content-Type rather than always claiming image/jpeg.
        _write_sidecar_mime(cached_path, content_type)
        # Opportunistic LRU sweep — if the cache directory has grown
        # past the soft cap, delete oldest entries until back under.
        _maybe_sweep_cache(cache_dir)
    except OSError:
        logger.warning("Poster cache write failed for %s", rating_key, exc_info=True)
        if tmp_name:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)

    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": f"public, max-age={_CACHE_MAX_AGE}"},
    )
