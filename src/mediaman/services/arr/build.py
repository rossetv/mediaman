"""Factory helpers for building Radarr / Sonarr / NZBGet clients from DB settings.

The single place that does "read URL + decrypt API key + construct
client", so no caller can forget to decrypt or accept an empty URL.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from mediaman.services.infra import get_string_setting

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient
    from mediaman.services.downloads.nzbget import NzbgetClient
    from mediaman.services.media_meta.plex import PlexClient

logger = logging.getLogger(__name__)


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


def build_radarr_from_db(conn: sqlite3.Connection, secret_key: str) -> ArrClient | None:
    creds = _read_arr_credentials(conn, "radarr", secret_key)
    if creds is None:
        return None
    from mediaman.services.arr.base import ArrClient
    from mediaman.services.arr.spec import RADARR_SPEC

    return ArrClient(RADARR_SPEC, *creds)


def build_sonarr_from_db(conn: sqlite3.Connection, secret_key: str) -> ArrClient | None:
    creds = _read_arr_credentials(conn, "sonarr", secret_key)
    if creds is None:
        return None
    from mediaman.services.arr.base import ArrClient
    from mediaman.services.arr.spec import SONARR_SPEC

    return ArrClient(SONARR_SPEC, *creds)


def build_plex_from_db(conn: sqlite3.Connection, secret_key: str) -> PlexClient | None:
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
    ``get_string_setting``.

    ``secret_key`` is required to decrypt ``nzbget_password``.
    """
    from mediaman.services.downloads.nzbget import NzbgetClient

    url = get_string_setting(conn, "nzbget_url")
    user = get_string_setting(conn, "nzbget_username")
    password = get_string_setting(conn, "nzbget_password", secret_key=secret_key)
    if not url or not user:
        return None
    return NzbgetClient(url, user, password)
