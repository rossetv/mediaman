"""OMDb ratings fetch. Best-effort: returns an empty dict when the key is missing, the
request fails, or OMDb has nothing; never raises.

SQLite connections must not cross threads, so callers dispatching to a thread pool must
read the key via get_omdb_key first and pass it to the workers.

OMDb only accepts the key as a query parameter and urllib3 logs request URLs at DEBUG,
so a ScrubFilter is attached to the urllib3 and mediaman loggers when the key is first
resolved.
"""

from __future__ import annotations

import logging
import sqlite3

import requests

from mediaman.core.scrub_filter import ScrubFilter, register_secret
from mediaman.services.infra import SafeHTTPClient, SafeHTTPError, get_string_setting

#: Base URL for the OMDb REST API.
OMDB_API_BASE_URL = "https://www.omdbapi.com"

# Module-level client + session so the connection pool is shared across
# calls.  Rebuilt on process restart; the OMDb API key is passed as a
# per-request query parameter (not stored in the session), so rotating the
# key takes effect immediately without needing to rebuild the session.
# A global is required: per-request instantiation would rebuild
# the TCP/TLS connection pool on every call, negating keep-alive benefits.
# Single-worker invariant: mediaman runs under a single Uvicorn worker;
# ``requests.Session`` is not written after construction (no cookie mutation,
# no header mutation, no auth adapter attachment), so no external lock is
# needed beyond what ``urllib3``'s internal pool already provides.
_OMDB_SESSION = requests.Session()
# pinned host (``www.omdbapi.com`` ∈ PINNED_EXTERNAL_HOSTS); deny-list validation only.
_OMDB_CLIENT = SafeHTTPClient(OMDB_API_BASE_URL, session=_OMDB_SESSION)

logger = logging.getLogger(__name__)


def _attach_scrub_filters(api_key: str) -> None:
    """Register *api_key* for redaction and attach the scrub filter to the urllib3 logger.

    Idempotent per key. urllib3.connectionpool logs the full request URL at DEBUG, which
    would expose apikey=<value>. The mediaman logger is covered by the global
    register_secret call.
    """
    ScrubFilter.attach("urllib3.connectionpool", secrets=[api_key])
    register_secret(api_key)


def get_omdb_key(conn: sqlite3.Connection, secret_key: str) -> str | None:
    """Return the OMDb API key from settings, or None. Read it in the request thread before
    dispatching to a thread pool, since SQLite connections must not cross threads.
    """
    return get_string_setting(conn, "omdb_api_key", secret_key=secret_key) or None


def fetch_ratings(
    title: str,
    year: int | None,
    media_type: str,
    *,
    conn: sqlite3.Connection | None = None,
    secret_key: str | None = None,
    omdb_key: str | None = None,
) -> dict[str, str]:
    """Return OMDb ratings (any subset of imdb, rt, metascore); never raises.

    Pass either omdb_key (preferred from thread-pool workers, since conn must not cross
    threads) or conn plus secret_key to read the key in place.
    """
    if omdb_key is None:
        if conn is None or secret_key is None:
            raise TypeError("fetch_ratings requires either omdb_key= or both conn= and secret_key=")
        omdb_key = get_omdb_key(conn, secret_key)
    if not omdb_key:
        return {}

    # Attach log scrubbing for this key value before any network call.
    _attach_scrub_filters(omdb_key)

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
    except (SafeHTTPError, requests.RequestException, ValueError):
        # ``Response.json`` raises ``json.JSONDecodeError`` (a ValueError
        # subclass, not a RequestException) for malformed bodies.
        # KeyError is not caught: ``resp.json()`` never raises it, so
        # swallowing it would mask a real bug in the caller or the
        # client internals.
        return {}
    if not isinstance(data, dict) or data.get("Response") != "True":
        return {}

    out: dict[str, str] = {}
    imdb = data.get("imdbRating")
    if isinstance(imdb, str) and imdb and imdb != "N/A":
        out["imdb"] = imdb
    meta = data.get("Metascore")
    if isinstance(meta, str) and meta and meta != "N/A":
        out["metascore"] = meta
    entries = data.get("Ratings")
    for r in entries if isinstance(entries, list) else []:
        if isinstance(r, dict) and r.get("Source") == "Rotten Tomatoes":
            value = r.get("Value")
            if isinstance(value, str) and value:
                out["rt"] = value
            break
    return out
