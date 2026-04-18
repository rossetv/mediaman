"""Factory helpers for building Radarr / Sonarr clients from DB settings.

Every route module used to have its own inline copy of the
"read URL + decrypt API key + construct client" dance. Those copies
drifted over time, making bugs local (e.g. one forgetting to decrypt,
another ignoring an empty URL). This module is the single source of
truth.
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.services.settings_reader import get_string_setting

logger = logging.getLogger("mediaman")


def build_radarr_from_db(conn: sqlite3.Connection, secret_key: str):
    """Return a ``RadarrClient`` or ``None`` if Radarr isn't configured.

    Looks up ``radarr_url`` / ``radarr_api_key`` and, if both are set,
    returns a constructed client. Import of ``RadarrClient`` is deferred
    so the services layer doesn't pay its cost when Radarr is disabled.
    """
    url = get_string_setting(conn, "radarr_url")
    key = get_string_setting(conn, "radarr_api_key", secret_key=secret_key)
    if not url or not key:
        return None
    from mediaman.services.radarr import RadarrClient
    return RadarrClient(url, key)


def build_sonarr_from_db(conn: sqlite3.Connection, secret_key: str):
    """Return a ``SonarrClient`` or ``None`` if Sonarr isn't configured."""
    url = get_string_setting(conn, "sonarr_url")
    key = get_string_setting(conn, "sonarr_api_key", secret_key=secret_key)
    if not url or not key:
        return None
    from mediaman.services.sonarr import SonarrClient
    return SonarrClient(url, key)
