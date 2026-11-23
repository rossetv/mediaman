"""Outbound poster fetch logic and pure URL/mime helpers.

Threat model: SSRF. An attacker who can influence the stored Plex URL, or inject a
Radarr/Sonarr poster URL, could point the proxy at an internal metadata endpoint, loopback
or a cloud credential service. The first line of defence is here:

* is_valid_rating_key rejects non-numeric and oversized keys before any URL construction or
  DB lookup.
* safe_mime stops a hostile CDN injecting Content-Type: text/html through the proxy (stored
  XSS).
* is_allowed_poster_host does exact hostname matching plus a DNS-resolved public-IP check
  via is_safe_outbound_url.
* sanitise_plex_url re-validates the stored plex_url on every request, since a
  settings-write compromise could otherwise swap it for a hostile target.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from time import monotonic
from urllib.parse import urlencode, urlparse

import requests
from fastapi.responses import Response

from mediaman.config import Config
from mediaman.crypto import decrypt_value
from mediaman.services.arr import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.infra import (
    SafeHTTPClient,
    SafeHTTPError,
    allowed_outbound_hosts,
    is_safe_outbound_url,
)
from mediaman.web.repository.poster import PosterArrIds, fetch_arr_ids, fetch_plex_credentials
from mediaman.web.routes.poster.cache import ALLOWED_IMAGE_MIMES

logger = logging.getLogger(__name__)

# Remote poster fetches get a tight 3 s read timeout and 4 MiB cap — a
# poster that doesn't download in 3 s is broken, and real posters run
# well under 1 MiB. The session is module-level so the connection pool
# is shared across requests; the per-request client wraps it with the
# SSRF allowlist derived from the current settings on every request so a
# just-saved integration host is honoured without a restart.
_POSTER_SESSION = requests.Session()
# Stub target for tests; production routes via _make_poster_client.
_POSTER_HTTP = SafeHTTPClient(
    session=_POSTER_SESSION,
    default_timeout=(3.0, 3.0),
    default_max_bytes=4 * 1024 * 1024,
)

# A poster no source could supply is not asked for again for a few minutes: failures are
# never written to the disk cache, so without this every page load would refetch it.
# rating_key -> monotonic time the pause ends.
_MISSING_POSTER_PAUSE_SECONDS = 180.0
_MISSING_POSTERS_MAX = 1024
_MISSING_POSTERS: dict[str, float] = {}

# Cards show posters at most a few hundred pixels wide; asking Plex for this size keeps an
# original of many megabytes under the body cap.
_POSTER_WIDTH = 600
_POSTER_HEIGHT = 900


def _make_poster_client(conn: sqlite3.Connection) -> SafeHTTPClient:
    """Return a SafeHTTPClient bound to conn's SSRF allowlist.

    The allowlist (PINNED_EXTERNAL_HOSTS plus the integration hosts in settings) is rebuilt
    on every request so a just-saved host is honoured without a restart; the session is
    shared with _POSTER_HTTP so the connection pool persists.

    Tests monkeypatch _POSTER_HTTP with a stub; if it is no longer the original
    SafeHTTPClient, the stub is returned unmodified.
    """
    if not isinstance(_POSTER_HTTP, SafeHTTPClient):
        return _POSTER_HTTP
    return SafeHTTPClient(
        session=_POSTER_SESSION,
        default_timeout=(3.0, 3.0),
        default_max_bytes=4 * 1024 * 1024,
        allowed_hosts=allowed_outbound_hosts(conn),
    )


# Only HTTPS poster URLs on the default port are accepted. The set of
# *which* hosts may be reached is owned entirely by the SafeHTTPClient
# allowlist (``allowed_outbound_hosts`` composed from
# ``PINNED_EXTERNAL_HOSTS``, the single canonical list of poster-CDN hosts:
# image.tmdb.org, m.media-amazon.com, images.amazon.com). This module keeps no
# second host table, since a duplicate allowlist could drift out of sync with
# the pinned set.
_POSTER_ALLOWED_PORT = 443


def is_valid_rating_key(rating_key: str) -> bool:
    """Return True only for a non-empty string of ASCII digits up to 12 characters, rejecting
    path-traversal sequences and long keys before they touch a URL template or filesystem
    path.
    """
    return bool(rating_key) and rating_key.isdigit() and len(rating_key) <= 12


def safe_mime(remote_type: str | None) -> str:
    """Coerce a remote Content-Type into a safe served mime type: values in ALLOWED_IMAGE_MIMES
    pass through, everything else (missing, malformed, text/html) becomes image/jpeg. The
    primary defence against a malicious CDN using the proxy for stored XSS.
    """
    if not remote_type:
        return "image/jpeg"
    base = remote_type.split(";", 1)[0].strip().lower()
    if base in ALLOWED_IMAGE_MIMES:
        return base
    return "image/jpeg"


def is_allowed_poster_host(url: str, *, allowed_hosts: frozenset[str]) -> bool:
    """Return True only for HTTPS poster URLs on an allowlisted host.

    The scheme/port guard for the Radarr/Sonarr poster fetch: HTTPS only, port 443 only
    (parsed.port is None for the implicit port), and a DNS-resolving public-IP check via
    is_safe_outbound_url with strict egress, which catches a rebind returning a private IP
    at request time.

    Host policy is not decided here: allowed_hosts is the SafeHTTPClient allowlist (which
    already merges PINNED_EXTERNAL_HOSTS), so there is one source of truth and a refused
    host fails before the transport call.
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
    # Port must be the implicit/explicit HTTPS default; ``parsed.port`` is
    # None when the URL omits the port.
    port = parsed.port if parsed.port is not None else _POSTER_ALLOWED_PORT
    if port != _POSTER_ALLOWED_PORT:
        return False
    # Delegate host policy to the canonical allowlist (pinned CDNs +
    # configured integrations) instead of a bespoke second table.
    return bool(is_safe_outbound_url(url, strict_egress=True, allowed_hosts=allowed_hosts))


def sanitise_plex_url(raw: str | None) -> str | None:
    """Return scheme://host[:port] if raw passes the SSRF and scheme checks. Runs on every
    poster request, since an attacker with settings-write access could rotate the stored
    plex_url after startup. Userinfo, non-http(s) schemes and anything the SSRF guard
    refuses give None.
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
    """Load and decrypt the Plex URL and token. Returns (plex_base, plex_token, None), or
    (None, None, error_response) when a field is absent or the URL fails the per-request
    SSRF re-check.
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


def _log_snippet(snippet: str, token: str | None = None, limit: int = 200) -> str:
    """Make an upstream body snippet safe to log: the token is redacted before the cut, so one
    straddling the limit cannot leak, and any line break is folded to a space so the body
    cannot forge log lines.
    """
    if token:
        snippet = snippet.replace(token, "***")
    return " ".join(snippet.split())[:limit]


# The safe client reports a transport error as a status-0 SafeHTTPError whose snippet names the
# exception class. A connection error or connect timeout means Plex is unreachable; a read
# timeout means it answered slowly, so the original is still worth asking for.
_CONNECT_FAILURE_SNIPPET = re.compile(r"transport error: (ConnectionError|ConnectTimeout)$")


def _is_connect_failure(exc: BaseException) -> bool:
    """True when a requests exception means Plex could not be reached at all."""
    return isinstance(exc, requests.ConnectionError | requests.ConnectTimeout)


def fetch_plex_poster(
    plex_base: str, plex_token: str, rating_key: str, *, http_client: SafeHTTPClient | None = None
) -> tuple[bytes | None, str]:
    """Fetch a poster from Plex; returns (content, content_type) or (None, 'image/jpeg').

    Asks Plex's photo transcoder for a card-sized copy first, so an original over the body cap
    still shows, and falls back to the original when the transcoder answers with an HTTP error
    or a rejected body or a read timeout. A connect failure (Plex unreachable) ends the fetch:
    the original would fail the same way, after the same retries. Both are GETs.
    http_client defaults to _POSTER_HTTP; callers that need the SSRF allowlist pass one from
    _make_poster_client.
    """
    client = http_client if http_client is not None else _POSTER_HTTP
    thumb_path = f"/library/metadata/{rating_key}/thumb"
    query = urlencode(
        {"width": _POSTER_WIDTH, "height": _POSTER_HEIGHT, "minSize": 1, "url": thumb_path}
    )
    for url in (f"{plex_base}/photo/:/transcode?{query}", f"{plex_base}{thumb_path}"):
        try:
            response = client.get(url, headers={"X-Plex-Token": plex_token})
            return response.content, safe_mime(response.headers.get("Content-Type"))
        except SafeHTTPError as exc:
            logger.warning(
                "Plex poster fetch failed for rating_key=%s (%s): %s",
                rating_key,
                exc.status_code,
                _log_snippet(exc.body_snippet, plex_token),
            )
            if exc.status_code == 0 and _CONNECT_FAILURE_SNIPPET.match(exc.body_snippet):
                break
        except requests.RequestException as exc:
            logger.warning(
                "Failed to fetch Plex poster for rating_key=%s", rating_key, exc_info=True
            )
            if _is_connect_failure(exc):
                break
    return None, "image/jpeg"


def _resolve_arr_poster_url(
    conn: sqlite3.Connection, row: PosterArrIds, config: Config
) -> tuple[str | None, str | None]:
    """Look up the Radarr/Sonarr poster URL for a stored media row; (None, None) when the row
    has no Arr ID or the lookup fails. Network errors are swallowed at warning level and the
    caller falls back to 404.
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
            # Single-item lookup — O(1) vs. O(N) full-library scan.
            movie = radarr_client.get_movie_by_id(radarr_id)
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
        # Single-item lookup — O(1) vs. O(N) full-library scan.
        series = sonarr_client.get_series_by_id(sonarr_id)
        return extract_poster_url(series.get("images")), title
    except (requests.RequestException, SafeHTTPError, ArrError):
        logger.warning("Failed to fetch Sonarr poster for id=%s", sonarr_id, exc_info=True)
    return None, title


def _fetch_allowed_poster_bytes(
    poster_url: str,
    *,
    allowed_hosts: frozenset[str],
    http_client: SafeHTTPClient | None = None,
) -> tuple[bytes | None, str | None]:
    """Fetch poster bytes after the scheme/host guard; (None, None) when the host is disallowed
    or the fetch fails. allowed_hosts is the SafeHTTPClient allowlist; http_client defaults
    to _POSTER_HTTP.
    """
    if not is_allowed_poster_host(poster_url, allowed_hosts=allowed_hosts):
        logger.warning("Refusing Radarr/Sonarr poster fetch for disallowed host: %s", poster_url)
        return None, None
    client = http_client if http_client is not None else _POSTER_HTTP
    try:
        response = client.get(poster_url)
        return response.content, safe_mime(response.headers.get("Content-Type"))
    except SafeHTTPError as exc:
        logger.warning(
            "Arr poster fetch refused/failed: %s (%s): %s",
            poster_url,
            exc.status_code,
            _log_snippet(exc.body_snippet),
        )
    except requests.RequestException:
        logger.warning("Failed to fetch arr poster from %s", poster_url, exc_info=True)
    return None, None


def fetch_arr_poster(
    conn: sqlite3.Connection,
    rating_key: str,
    config: Config,
    *,
    http_client: SafeHTTPClient | None = None,
) -> tuple[bytes | None, str | None]:
    """Try to fetch a poster from Radarr/Sonarr TMDB data for a media item.

    Uses the stored radarr_id / sonarr_id on the media_items row rather than the title:
    matching by title would let two items sharing a title poison each other's cached poster.
    Returns (content_bytes, content_type) or (None, None), and the caller responds 404
    rather than guess a replacement.
    """
    row = fetch_arr_ids(conn, rating_key)
    if not row:
        return None, None

    poster_url, _title = _resolve_arr_poster_url(conn, row, config)
    if not poster_url:
        return None, None
    return _fetch_allowed_poster_bytes(
        poster_url,
        allowed_hosts=allowed_outbound_hosts(conn),
        http_client=http_client,
    )


def resolve_poster_content(
    conn: sqlite3.Connection, rating_key: str, plex_base: str, plex_token: str, config: Config
) -> tuple[bytes | None, str, Response | None]:
    """Fetch poster bytes from Plex with Radarr/Sonarr fallback.

    Returns (content, content_type, None), or (None, '', 404_response) when no source has a
    poster. One SafeHTTPClient is built per request and threaded through both Plex and the
    Arr fallback, so the freshly composed SSRF allowlist reaches every fetch and a
    misconfigured plex_url cannot mask an allowlist gap.
    """
    now = monotonic()
    if _MISSING_POSTERS.get(rating_key, 0.0) > now:
        return None, "", Response(status_code=404)
    poster_client = _make_poster_client(conn)
    content, content_type = fetch_plex_poster(
        plex_base, plex_token, rating_key, http_client=poster_client
    )
    # Fallback: fetch poster from Radarr/Sonarr via TMDB if Plex has none
    if content is None:
        content, fallback_type = fetch_arr_poster(
            conn, rating_key, config, http_client=poster_client
        )
        if content is None:
            logger.info("Poster unavailable for rating_key=%s — returning 404", rating_key)
            if len(_MISSING_POSTERS) >= _MISSING_POSTERS_MAX:
                _MISSING_POSTERS.clear()
            _MISSING_POSTERS[rating_key] = now + _MISSING_POSTER_PAUSE_SECONDS
            return None, "", Response(status_code=404)
        content_type = fallback_type or "image/jpeg"
    return content, content_type, None
