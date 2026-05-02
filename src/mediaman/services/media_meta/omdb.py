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

Logging note
------------
The OMDb REST API only accepts the API key as a query string parameter.
``urllib3`` logs request URLs at DEBUG, so the key would otherwise leak
into ``mediaman.log`` in any deployment that enables DEBUG-level
logging.  We install a logging filter on the urllib3 connection logger
that scrubs ``apikey=`` from messages before they're emitted.
"""

from __future__ import annotations

import logging
import re
import sqlite3

import requests

from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError
from mediaman.services.infra.settings_reader import get_string_setting

#: Base URL for the OMDb REST API.
OMDB_API_BASE_URL = "https://www.omdbapi.com"

# Module-level client + session so the connection pool is shared across
# calls. ``SafeHTTPClient`` accepts a ``session`` kwarg so callers can
# provide their own pool — the previous build constructed the client
# without one, which left every call using a fresh connection.
_OMDB_SESSION = requests.Session()
_OMDB_CLIENT = SafeHTTPClient(OMDB_API_BASE_URL, session=_OMDB_SESSION)

logger = logging.getLogger("mediaman")


# ---------------------------------------------------------------------------
# Scrub ``apikey=`` from any urllib3/requests log messages so a DEBUG-level
# logging configuration cannot leak the key into the on-disk log.
# ---------------------------------------------------------------------------
_APIKEY_QS_RE = re.compile(r"apikey=[^&\s'\"]*", re.IGNORECASE)


class _ScrubApiKeyFilter(logging.Filter):
    """Logging filter that replaces ``apikey=<value>`` with ``apikey=<redacted>``.

    Attached to the ``urllib3.connectionpool`` logger at module import.
    DEBUG log records on that logger include the full request URL, which
    on OMDb means the key.  The filter rewrites the message in place so
    nothing downstream (file handler, syslog) sees the secret.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and "apikey=" in record.msg.lower():
                record.msg = _APIKEY_QS_RE.sub("apikey=<redacted>", record.msg)
            if record.args and isinstance(record.args, tuple):
                record.args = tuple(
                    _APIKEY_QS_RE.sub("apikey=<redacted>", a) if isinstance(a, str) else a
                    for a in record.args
                )
        except Exception:
            # A logging filter that raises would silence the log entirely —
            # drop quietly and let the (possibly unscrubbed) record through.
            return True
        return True


# Attach to the urllib3 connection-pool logger (where the request URL is
# logged at DEBUG) and to ``mediaman`` itself for any caller that ever
# stringifies a SafeHTTPError carrying the URL.  The filter is idempotent
# — repeated module imports won't double-attach because ``addFilter``
# silently no-ops on an already-present instance reference.
_omdb_apikey_filter = _ScrubApiKeyFilter()
logging.getLogger("urllib3.connectionpool").addFilter(_omdb_apikey_filter)
logger.addFilter(_omdb_apikey_filter)


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
    except (SafeHTTPError, requests.RequestException, ValueError, KeyError):
        # ValueError covers ``Response.json``'s ``json.JSONDecodeError``
        # (a subclass of ValueError, NOT RequestException) which the
        # bare-Exception catch used to swallow alongside genuine
        # programming errors.  KeyError is kept for the rare case where
        # SafeHTTPClient internals raise on a missing dict key.
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
