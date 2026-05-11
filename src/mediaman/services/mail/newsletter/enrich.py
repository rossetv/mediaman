"""Arr-state enrichment for the recommendation cards embedded in the newsletter."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, cast

import requests

from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import (
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
)
from mediaman.services.infra.http import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.state import ArrCaches

logger = logging.getLogger(__name__)


def _annotate_rec_download_states(
    rec_items: list[dict],
    conn: sqlite3.Connection,
    secret_key: str,
) -> None:
    """Annotate recommendation items in place with their Arr download state.

    Populates ``item["download_state"]`` (``in_library`` / ``partial`` /
    ``downloading`` / ``queued``) by consulting Radarr and Sonarr caches.
    Silently skips items with no ``tmdb_id``.
    """
    radarr_client = build_radarr_from_db(conn, secret_key)
    sonarr_client = build_sonarr_from_db(conn, secret_key)
    try:
        radarr_cache = build_radarr_cache(radarr_client)
    except (SafeHTTPError, requests.RequestException, ArrError):
        logger.warning(
            "Failed to build Radarr cache for newsletter; skipping download states", exc_info=True
        )
        radarr_cache = build_radarr_cache(None)
    try:
        sonarr_cache = build_sonarr_cache(sonarr_client)
    except (SafeHTTPError, requests.RequestException, ArrError):
        logger.warning(
            "Failed to build Sonarr cache for newsletter; skipping download states", exc_info=True
        )
        sonarr_cache = build_sonarr_cache(None)

    caches = cast("ArrCaches", {**radarr_cache, **sonarr_cache})
    for item in rec_items:
        tmdb_id = item.get("tmdb_id")
        if not tmdb_id:
            continue
        state = compute_download_state(
            item.get("media_type") or "movie",
            tmdb_id,
            caches,
        )
        if state is not None:
            item["download_state"] = state
