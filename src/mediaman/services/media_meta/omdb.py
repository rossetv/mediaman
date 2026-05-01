"""OMDb ratings fetch — extracted from the /download flow.

Best-effort. Returns an empty dict when the key is missing, the
request fails, or OMDb has nothing useful. Never raises.

Threading note
--------------
SQLite connections must not be shared across threads.  ``fetch_ratings``
reads the OMDb API key from the DB but must only be called from the thread
that owns *conn*.  Callers that dispatch work to a thread pool must read the
key via :func:`get_omdb_key` *before* submitting tasks and pass the resolved
key string to worker callables directly (see ``search.py``).
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


def get_omdb_key(conn: sqlite3.Connection, secret_key: str) -> str | None:
    """Return the OMDb API key from settings, or ``None`` if not configured.

    Read this in the *request thread* before dispatching to a thread pool —
    SQLite connections must not cross thread boundaries (finding 32).

    Delegates to :func:`~mediaman.services.infra.settings_reader.get_string_setting`
    so the decrypt-and-unwrap logic is not duplicated here.
    """
    return get_string_setting(conn, "omdb_api_key", secret_key=secret_key) or None


# Keep the old private name as an alias so existing internal callers and tests
# continue to work without change.
_get_key = get_omdb_key


def fetch_ratings(
    title: str,
    year: int | None,
    media_type: str,
    *,
    conn: sqlite3.Connection | None = None,
    secret_key: str | None = None,
    omdb_key: str | None = None,
) -> dict[str, str]:
    """Return ratings from OMDb.

    Keys in the returned dict (any subset): ``imdb``, ``rt``, ``metascore``.
    Missing values are omitted. Never raises.

    Callers must supply either:

    * ``omdb_key`` — the already-resolved API key string (preferred when
      calling from a thread-pool worker, since *conn* must not be used across
      threads), or
    * ``conn`` + ``secret_key`` — the DB connection and master key, from which
      the OMDb key is read in-place.  Only safe when called from the thread
      that owns *conn*.
    """
    if omdb_key is None:
        if conn is None or secret_key is None:
            raise TypeError("fetch_ratings requires either omdb_key= or both conn= and secret_key=")
        omdb_key = get_omdb_key(conn, secret_key)
    if not omdb_key:
        return {}

    params: dict[str, object] = {
        "apikey": omdb_key,
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
