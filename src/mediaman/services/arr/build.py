"""Factory helpers for building Radarr / Sonarr / NZBGet clients from DB settings.

Every route module used to have its own inline copy of the
"read URL + decrypt API key + construct client" dance. Those copies
drifted over time, making bugs local (e.g. one forgetting to decrypt,
another ignoring an empty URL). This module is the single source of
truth.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from mediaman.services.infra.settings_reader import get_string_setting

if TYPE_CHECKING:
    from mediaman.services.arr.radarr import RadarrClient
    from mediaman.services.arr.sonarr import SonarrClient
    from mediaman.services.downloads.nzbget import NzbgetClient
    from mediaman.services.media_meta.plex import PlexClient

logger = logging.getLogger("mediaman")


def _read_arr_credentials(
    conn: sqlite3.Connection, service: str, secret_key: str
) -> tuple[str, str] | None:
    """Read ``{service}_url`` / ``{service}_api_key`` from settings.

    Returns the ``(url, api_key)`` pair or ``None`` if either is missing.
    The API key column may be encrypted; ``secret_key`` is used to
    decrypt it. Centralised here so the per-service wrappers stay one
    line each — and so a future setting-name rename touches one place.
    """
    url = get_string_setting(conn, f"{service}_url")
    key = get_string_setting(conn, f"{service}_api_key", secret_key=secret_key)
    if not url or not key:
        return None
    return url, key


def build_radarr_from_db(conn: sqlite3.Connection, secret_key: str) -> RadarrClient | None:
    """Return a ``RadarrClient`` or ``None`` if Radarr isn't configured.

    Looks up ``radarr_url`` / ``radarr_api_key`` and, if both are set,
    returns a constructed client. Import of ``RadarrClient`` is deferred
    so the services layer doesn't pay its cost when Radarr is disabled.
    """
    creds = _read_arr_credentials(conn, "radarr", secret_key)
    if creds is None:
        return None
    from mediaman.services.arr.radarr import RadarrClient

    return RadarrClient(*creds)


def build_sonarr_from_db(conn: sqlite3.Connection, secret_key: str) -> SonarrClient | None:
    """Return a ``SonarrClient`` or ``None`` if Sonarr isn't configured."""
    creds = _read_arr_credentials(conn, "sonarr", secret_key)
    if creds is None:
        return None
    from mediaman.services.arr.sonarr import SonarrClient

    return SonarrClient(*creds)


def build_arr_client(
    conn: sqlite3.Connection,
    service: str,
    secret_key: str,
) -> RadarrClient | SonarrClient | None:
    """Build a Radarr or Sonarr client from DB settings. Returns None if unconfigured.

    ``secret_key`` is required to decrypt the stored API key.
    """
    if service == "radarr":
        return build_radarr_from_db(conn, secret_key)
    if service == "sonarr":
        return build_sonarr_from_db(conn, secret_key)
    return None


def build_plex_from_db(conn: sqlite3.Connection, secret_key: str) -> PlexClient | None:
    """Return a ``PlexClient`` or ``None`` if Plex isn't configured."""
    url = get_string_setting(conn, "plex_url")
    token = get_string_setting(conn, "plex_token", secret_key=secret_key)
    if not url or not token:
        return None
    from mediaman.services.media_meta.plex import PlexClient

    return PlexClient(url, token)


def build_nzbget_from_db(
    conn: sqlite3.Connection,
    secret_key: str,
) -> NzbgetClient | None:
    """Return an ``NzbgetClient`` or ``None`` if NZBGet isn't configured.

    Reads ``nzbget_url``, ``nzbget_username``, and ``nzbget_password`` (which
    may be encrypted) from DB settings via
    :func:`~mediaman.services.infra.settings_reader.get_string_setting`.

    ``secret_key`` is required to decrypt ``nzbget_password``.
    """
    from mediaman.services.downloads.nzbget import NzbgetClient

    url = get_string_setting(conn, "nzbget_url")
    user = get_string_setting(conn, "nzbget_username")
    password = get_string_setting(conn, "nzbget_password", secret_key=secret_key)
    if not url or not user:
        return None
    return NzbgetClient(url, user, password)
