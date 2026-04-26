"""OMDb ratings fetch — extracted from the /download flow.

Best-effort. Returns an empty dict when the key is missing, the
request fails, or OMDb has nothing useful. Never raises.
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.services.infra.http_client import SafeHTTPClient
from mediaman.services.infra.settings_reader import get_string_setting

#: Base URL for the OMDb REST API.
OMDB_API_BASE_URL = "https://www.omdbapi.com"

# Module-level client so the connection pool is shared across calls.
_OMDB_CLIENT = SafeHTTPClient(OMDB_API_BASE_URL)

logger = logging.getLogger("mediaman")


def _get_key(conn: sqlite3.Connection, secret_key: str) -> str | None:
    """Return the OMDb API key from settings, or ``None`` if not configured.

    Delegates to :func:`~mediaman.services.infra.settings_reader.get_string_setting`
    so the decrypt-and-unwrap logic is not duplicated here.
    """
    return get_string_setting(conn, "omdb_api_key", secret_key=secret_key) or None


def fetch_ratings(
    title: str,
    year: int | None,
    media_type: str,
    *,
    conn: sqlite3.Connection,
    secret_key: str,
) -> dict[str, str]:
    """Return ratings from OMDb.

    Keys in the returned dict (any subset): ``imdb``, ``rt``, ``metascore``.
    Missing values are omitted. Never raises.
    """
    key = _get_key(conn, secret_key)
    if not key:
        return {}

    params = {
        "apikey": key,
        "t": title,
        "type": "movie" if media_type == "movie" else "series",
    }
    if year:
        params["y"] = year

    try:
        resp = _OMDB_CLIENT.get("/", params=params, timeout=(5.0, 5.0))
        data = resp.json()
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("Response") != "True":
        return {}

    out: dict[str, str] = {}
    imdb = data.get("imdbRating")
    if imdb and imdb != "N/A":
        out["imdb"] = imdb
    meta = data.get("Metascore")
    if meta and meta != "N/A":
        out["metascore"] = meta
    for r in data.get("Ratings", []):
        if r.get("Source") == "Rotten Tomatoes":
            out["rt"] = r["Value"]
            break
    return out
