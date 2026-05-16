"""Outbound poster fetch logic for Plex and Radarr/Sonarr.

This module owns the HTTP calls (and per-request SSRF-allowlist client
construction) used by the poster proxy. Pure validation helpers
(``is_valid_rating_key``, ``safe_mime``, ``is_allowed_poster_host``,
``sanitise_plex_url``) live in :mod:`._validation` so they can be
imported by callers that do not need the HTTP plumbing.

Threat model
------------
The primary concern for poster fetching is SSRF (Server-Side Request
Forgery).  An attacker who can influence the Plex URL stored in the
database — or who can inject a Radarr/Sonarr poster URL — could redirect
the proxy to an internal metadata endpoint, loopback address, or cloud
credential service.  The validation helpers in :mod:`._validation` form
the first line of defence; this module wraps every outbound call in a
:class:`SafeHTTPClient` bound to the current settings' allowlist.
"""

from __future__ import annotations

import logging
import sqlite3

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
)
from mediaman.web.repository.poster import PosterArrIds, fetch_arr_ids, fetch_plex_credentials
from mediaman.web.routes.poster._validation import (
    is_allowed_poster_host,
    safe_mime,
    sanitise_plex_url,
)

logger = logging.getLogger(__name__)

# Remote poster fetches get a tight 3 s read timeout and 4 MiB cap — a
# poster that doesn't download in 3 s is broken, and real posters run
# well under 1 MiB. The session is module-level so the connection pool
# is shared across requests; the per-request client wraps it with the
# SSRF allowlist derived from the current settings on every request so a
# just-saved integration host is honoured without a restart.
_POSTER_SESSION = requests.Session()
# Back-compat stub-target for tests; production routes via _make_poster_client.
_POSTER_HTTP = SafeHTTPClient(
    session=_POSTER_SESSION,
    default_timeout=(3.0, 3.0),
    default_max_bytes=4 * 1024 * 1024,
)


def _make_poster_client(conn: sqlite3.Connection) -> SafeHTTPClient:
    """Return a :class:`SafeHTTPClient` bound to *conn*'s SSRF allowlist.

    The SSRF allowlist is composed from the current settings on every
    request so a just-saved integration host is honoured without a restart.
    It is the union of :data:`PINNED_EXTERNAL_HOSTS` and the configured
    integration hosts from the ``settings`` table.

    The session is shared with :data:`_POSTER_HTTP` so the connection
    pool persists across requests; only the allowlist context is
    per-call.

    Existing tests monkeypatch ``_POSTER_HTTP`` itself to a stub. If
    the module-level attribute has been swapped out (i.e. it is no
    longer the original :class:`SafeHTTPClient`), return that stub
    unmodified so those tests continue to observe their patched
    transport. Production code keeps the original instance, so the
    fresh, allowlist-bound client is constructed normally.
    """
    if not isinstance(_POSTER_HTTP, SafeHTTPClient):
        return _POSTER_HTTP
    return SafeHTTPClient(
        session=_POSTER_SESSION,
        default_timeout=(3.0, 3.0),
        default_max_bytes=4 * 1024 * 1024,
        allowed_hosts=allowed_outbound_hosts(conn),
    )


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

    *http_client* defaults to :data:`_POSTER_HTTP`; callers that need the
    SSRF allowlist enforced should pass a client built via
    :func:`_make_poster_client`. Tests substitute a stub.
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


def _fetch_allowed_poster_bytes(
    poster_url: str, *, http_client: SafeHTTPClient | None = None
) -> tuple[bytes | None, str | None]:
    """Fetch poster bytes from *poster_url* after host-allow-list check.

    Returns ``(content_bytes, content_type)`` or ``(None, None)`` when
    the host is disallowed or the fetch fails.

    *http_client* defaults to :data:`_POSTER_HTTP`; callers that need the
    SSRF allowlist enforced pass a client built via
    :func:`_make_poster_client`.
    """
    if not is_allowed_poster_host(poster_url):
        logger.warning("Refusing Radarr/Sonarr poster fetch for disallowed host: %s", poster_url)
        return None, None
    client = http_client if http_client is not None else _POSTER_HTTP
    try:
        resp = client.get(poster_url)
        return resp.content, safe_mime(resp.headers.get("Content-Type"))
    except SafeHTTPError as exc:
        logger.warning("Arr poster fetch refused/failed: %s (%s)", poster_url, exc.status_code)
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

    Looks up the stored ``radarr_id`` / ``sonarr_id`` on the
    ``media_items`` row for this Plex rating key and fetches the
    poster for that exact Arr record.  Matching by ID rather than
    title prevents a cache-poisoning primitive where two media items share a
    title but have different Arr IDs — matching by ID ensures each item's
    poster is fetched from its own Arr record only.

    Returns ``(content_bytes, content_type)`` or ``(None, None)`` when
    no source can supply the poster.  The caller responds with 404 in
    that case rather than guess a replacement.

    *http_client* threads through to :func:`_fetch_allowed_poster_bytes`
    so the SSRF allowlist reaches the actual transport call.
    """
    row = fetch_arr_ids(conn, rating_key)
    if not row:
        return None, None

    poster_url, _title = _resolve_arr_poster_url(conn, row, config)
    if not poster_url:
        return None, None
    return _fetch_allowed_poster_bytes(poster_url, http_client=http_client)


def resolve_poster_content(
    conn: sqlite3.Connection, rating_key: str, plex_base: str, plex_token: str, config: Config
) -> tuple[bytes | None, str, Response | None]:
    """Fetch poster bytes from Plex with Radarr/Sonarr fallback.

    Returns ``(content, content_type, None)`` on success, or
    ``(None, '', 404_response)`` when no source has a poster.

    Builds one :class:`SafeHTTPClient` per request via
    :func:`_make_poster_client` so the SSRF allowlist composed from the
    current settings reaches every outbound poster fetch so a just-saved
    integration host is honoured without a restart. The same client is
    threaded through Plex and the Radarr/Sonarr fallback so a misconfigured
    `plex_url` cannot mask an allowlist gap.
    """
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
            return None, "", Response(status_code=404)
        content_type = fallback_type or "image/jpeg"
    return content, content_type, None
