"""Plex client construction + per-settings client cache.

The previous code rebuilt :class:`PlexClient` on every
:func:`run_library_sync` call (every 30 min by default) — each rebuild
re-validates the URL via the SSRF guard and decrypts the stored token.
The cache here reuses the existing client until the underlying settings
change, keyed on a hash of (raw ``plex_url``, raw encrypted ``plex_token``
row). The hash deliberately uses the raw encrypted token so we never
need to decrypt just to check freshness.

Returns a :class:`PlexClientBundle` (Plex client plus the library
metadata derived from it) so callers can drive the scan engine without
re-querying Plex for library shape on every invocation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from typing import TYPE_CHECKING, NamedTuple

from mediaman.services.arr.build import build_plex_from_db as _build_plex
from mediaman.services.infra import SSRFRefused

if TYPE_CHECKING:
    from mediaman.services.media_meta.plex import PlexClient


class PlexClientBundle(NamedTuple):
    """Return type for :func:`build_plex_client`.

    NamedTuple (not TypedDict) because callers use positional tuple unpacking
    (``plex, lib_ids, lib_types, lib_titles = result``), which TypedDict
    doesn't support.
    """

    plex: PlexClient
    lib_ids: list[str]
    lib_types: dict[str, str]
    lib_titles: dict[str, str]


logger = logging.getLogger(__name__)

# rationale: module-level mutable cache is required so the scheduled
# library-sync (every 30 min) reuses the previous PlexClient instead of
# re-validating the URL via the SSRF guard and decrypting the token on
# every tick. The threading.Lock below guards every read and write; see
# CODE_GUIDELINES §8.5.
_PLEX_CLIENT_CACHE: dict[str, PlexClient] = {}
_PLEX_CLIENT_CACHE_LOCK = threading.Lock()


def _plex_settings_fingerprint(conn: sqlite3.Connection) -> str | None:
    """Return a stable fingerprint of the Plex-related settings.

    The fingerprint is an SHA-256 hash of the raw stored ``plex_url``
    plus the **raw encrypted** ``plex_token`` row (no decryption
    needed). Returns ``None`` when either setting is missing — callers
    treat that as "Plex not configured" and skip the cache.
    """
    url_row = conn.execute("SELECT value FROM settings WHERE key='plex_url'").fetchone()
    tok_row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
    if not url_row or not url_row["value"] or not tok_row or not tok_row["value"]:
        return None
    h = hashlib.sha256()
    h.update(b"plex_url:")
    h.update(str(url_row["value"]).encode("utf-8"))
    h.update(b"\x00plex_token:")
    h.update(str(tok_row["value"]).encode("utf-8"))
    return h.hexdigest()


def _reset_plex_client_cache() -> None:
    """Clear the cached Plex client. Test helper; safe to call any time."""
    with _PLEX_CLIENT_CACHE_LOCK:
        _PLEX_CLIENT_CACHE.clear()


def _load_library_ids(conn: sqlite3.Connection) -> list[str]:
    """Read plex_libraries from settings, returning [] on missing or corrupt JSON."""
    row = conn.execute("SELECT value FROM settings WHERE key='plex_libraries'").fetchone()
    if not row:
        return []
    try:
        parsed = json.loads(row["value"])
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        logger.warning("plex_libraries setting contains invalid JSON — scanning no libraries")
        return []


def _get_or_build_plex(conn: sqlite3.Connection, secret_key: str) -> PlexClient | None:
    """Return a cached PlexClient, rebuilding only when settings change.

    Cache key: SHA-256 of (raw ``plex_url`` value, raw encrypted
    ``plex_token`` value). Any settings change invalidates the entry.

    Returns ``None`` when Plex is unconfigured. Re-raises ``SSRFRefused``
    from the SSRF guard to the caller so it can log + skip.

    Avoids the per-invocation cost of SSRF re-validation and token
    decryption on the hot ``run_library_sync`` path.
    """
    fp = _plex_settings_fingerprint(conn)
    if fp is None:
        # No usable settings — also clear any stale cached client.
        with _PLEX_CLIENT_CACHE_LOCK:
            _PLEX_CLIENT_CACHE.clear()
        return None

    with _PLEX_CLIENT_CACHE_LOCK:
        cached = _PLEX_CLIENT_CACHE.get(fp)
    if cached is not None:
        return cached

    plex = _build_plex(conn, secret_key)
    if plex is None:
        return None

    with _PLEX_CLIENT_CACHE_LOCK:
        # Drop other entries: at most one Plex configuration is in
        # use at a time and we don't want to leak old clients.
        _PLEX_CLIENT_CACHE.clear()
        _PLEX_CLIENT_CACHE[fp] = plex
    return plex


def build_plex_client(conn: sqlite3.Connection, secret_key: str) -> PlexClientBundle | None:
    """Build a PlexClient and resolve library metadata from DB settings.

    Returns a ``(plex, lib_ids, lib_types, lib_titles)`` tuple, or ``None``
    if the required ``plex_url`` / ``plex_token`` settings are absent
    **or** if the configured Plex URL fails the SSRF guard at use-time.

    The caller is responsible for any filtering or further configuration
    (disk thresholds, *arr clients, etc.) before constructing a ScanEngine.

    PlexClient construction is delegated to
    :func:`mediaman.services.arr.build.build_plex_from_db` to avoid
    duplicating the URL/token lookup and decrypt logic. The
    ``PlexClient`` constructor itself revalidates the configured URL,
    so a stored URL that has since started resolving to an internal
    or metadata address is refused here rather than at the first
    network call. The constructed client is cached at module scope
    keyed on the settings fingerprint so subsequent calls with the
    same configuration reuse it.
    """
    try:
        plex = _get_or_build_plex(conn, secret_key)
    except SSRFRefused:
        # PlexClient constructor refused the URL (SSRF guard). Log
        # without surfacing the URL itself — it may carry topology
        # information — and skip the scan rather than crash.
        logger.exception(
            "Plex client build refused by SSRF guard — verify plex_url in settings. Scan skipped."
        )
        return None
    if plex is None:
        return None

    lib_ids = _load_library_ids(conn)
    plex_libs = plex.get_libraries()
    lib_types: dict[str, str] = {lib["id"]: lib["type"] for lib in plex_libs}
    lib_titles: dict[str, str] = {lib["id"]: lib["title"].lower() for lib in plex_libs}

    return PlexClientBundle(plex, lib_ids, lib_types, lib_titles)
