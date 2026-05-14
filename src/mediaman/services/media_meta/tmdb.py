"""Unified TMDB client — search, details, and shape helpers.

Collapses the four previously-duplicated TMDB integrations (download
confirmation, OpenAI recommendation enrichment, search detail modal,
deleted-item poster fill) into a single class. All callers read the
bearer token through :func:`services.settings_reader.get_string_setting`
so token decrypt behaviour stays consistent.

The TMDB response-shape ``TypedDict``\\ s and the pure shaping helpers
(``shape_card`` / ``shape_detail``) live in the sibling
:mod:`._tmdb_shapes` module — they are network-free transforms over the
TMDB JSON payload. They are re-exported here, and :class:`TmdbClient`
re-binds the helpers as static methods, so the historical
``from ...tmdb import TmdbCard`` / ``TmdbClient.shape_card(...)`` call
sites keep working unchanged.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any

import requests

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError, get_string_setting

from ._tmdb_shapes import (
    TmdbCard,
    TmdbDetail,
    TmdbDetailsPayload,
    TmdbSearchResult,
    shape_card,
    shape_detail,
)

__all__ = [
    "TmdbCard",
    "TmdbClient",
    "TmdbDetail",
    "TmdbDetailsPayload",
    "TmdbSearchResult",
]

logger = logging.getLogger(__name__)

_BASE = "https://api.themoviedb.org/3"

# ---------------------------------------------------------------------------
# Module-level client cache.
#
# ``TmdbClient.from_db`` was previously called per request, which built a
# brand-new ``requests.Session`` every time.  That meant every call paid
# DNS + TLS handshake costs against ``api.themoviedb.org`` instead of
# reusing the connection pool.  We cache one client per (token, timeout)
# pair so multiple callers share the same session.
#
# Max size: 4 entries — one per timeout variant with headroom for token
# rotation (the old token's entry ages out as soon as the new one is used).
# Beyond 4 entries the oldest is evicted so the cache cannot grow without
# bound if callers vary ``timeout`` unexpectedly.  Rebuilt on process restart.
# ---------------------------------------------------------------------------
_CLIENT_CACHE: dict[tuple[str, float], TmdbClient] = {}
_CLIENT_CACHE_LOCK = threading.Lock()
_CLIENT_CACHE_MAXSIZE = 4


# H71: TMDB attribution notice
# TMDB requires that all products using their API display the TMDB logo
# and a "This product uses the TMDB API but is not endorsed or certified
# by TMDB" attribution notice. Poster image requests to image.tmdb.org
# are standard CDN fetches — they are not tracking pixels. No personal
# data is embedded in the URLs generated here. See the TMDB API Terms of
# Use at https://www.themoviedb.org/terms-of-use for the full attribution
# requirements.


class TmdbClient:
    """Thin wrapper around ``api.themoviedb.org/3``.

    Uses a Bearer read-token fetched from settings (key
    ``'tmdb_read_token'``, may be encrypted). Constructor fails closed —
    :meth:`from_db` returns ``None`` when no token is configured or
    decryption fails.
    """

    def __init__(self, read_token: str, *, timeout: float = 10.0) -> None:
        self._token = read_token
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {read_token}",
            "Accept": "application/json",
        }
        self._session = requests.Session()
        # Per-call timeout is kept small — TMDB is expected to be snappy.
        # pinned hosts (api.themoviedb.org / image.tmdb.org); deny-list validation only.
        self._http = SafeHTTPClient(
            _BASE,
            session=self._session,
            default_timeout=(5.0, float(timeout)),
        )

    @property
    def headers(self) -> dict[str, str]:
        """Return a copy of the auth headers for use with raw requests calls."""
        return dict(self._headers)

    @classmethod
    def from_db(
        cls,
        conn: sqlite3.Connection,
        secret_key: str,
        *,
        timeout: float = 10.0,
    ) -> TmdbClient | None:
        """Factory that reads the read-token from the settings table.

        Returns ``None`` if the token is missing or cannot be decrypted —
        callers must handle the absence gracefully, not raise.

        Subsequent calls with the same (token, timeout) pair return the
        same cached :class:`TmdbClient` so the underlying
        :class:`requests.Session` (and its TLS connection pool) is shared
        across requests.  When the operator rotates the token a fresh
        client is built automatically because the cache key changes.

        ``timeout`` is the default request timeout in seconds — the
        default (10.0) is the upper bound of the previous inline
        timeouts; call sites that historically used 5s can still pass a
        lower value if they need to stay snappy.
        """
        token = get_string_setting(conn, "tmdb_read_token", secret_key=secret_key, default="")
        if not token:
            return None
        cache_key = (token, float(timeout))
        with _CLIENT_CACHE_LOCK:
            cached = _CLIENT_CACHE.get(cache_key)
            if cached is not None:
                return cached
            client = cls(token, timeout=timeout)
            # Evict the oldest entry when the cache is full so it cannot
            # grow without bound if callers vary the ``timeout`` parameter.
            if len(_CLIENT_CACHE) >= _CLIENT_CACHE_MAXSIZE:
                oldest_key = next(iter(_CLIENT_CACHE))
                del _CLIENT_CACHE[oldest_key]
            _CLIENT_CACHE[cache_key] = client
            return client

    # ------------------------------------------------------------------
    # Network calls
    # ------------------------------------------------------------------

    # rationale: returns resp.json() whose runtime type varies per endpoint
    # (dict for single-item endpoints, list for collection endpoints). Each
    # public method narrows the result via .get() or isinstance guards and
    # returns a fully-typed value; annotating _get as Any avoids cascading
    # casts across every caller while keeping all public API return types
    # concrete.
    def _get(self, path: str, params: dict[str, object] | None = None) -> Any:
        """Perform an authenticated GET against *path* and return the parsed JSON.

        Centralises the ``self._http.get(..., headers=self._headers, params=...)``
        repetition shared by every public network method.  Propagates all
        exceptions to the caller — each public method has its own ``except``
        clause that decides whether to return ``None`` or ``[]``.

        Raises :class:`ValueError` when the response body is not valid
        JSON (e.g. TMDB returned an HTML error page during an outage).
        ``requests.Response.json`` raises ``ValueError`` (a subclass of
        ``json.JSONDecodeError``) — neither subclass of
        :class:`requests.RequestException` — so the per-method ``except``
        clauses must cover it explicitly via :class:`ValueError`.
        """
        return self._http.get(path, headers=self._headers, params=params or {}).json()

    @staticmethod
    def _log_request_failure(label: str, exc: Exception) -> None:
        """Log a TMDB request failure at the right level.

        * 401/403 → WARNING — operator-actionable (token wrong/expired).
        * 5xx     → ERROR — TMDB-side outage worth paging on.
        * Other   → DEBUG — transient network error or 4xx that we can
          safely swallow.

        Callers still return None/[] regardless of severity; the only
        difference is what the operator sees.
        """
        if isinstance(exc, SafeHTTPError):
            if exc.status_code in (401, 403):
                logger.warning(
                    "TMDB auth failure (%d) — check tmdb_read_token: %s", exc.status_code, label
                )
                return
            if 500 <= exc.status_code < 600:
                logger.error("TMDB %s server error (%d): %s", label, exc.status_code, exc)
                return
        logger.debug("TMDB %s failed: %s", label, exc)

    def is_reachable(self) -> bool:
        """Return True if the TMDB API is reachable and the token is valid."""
        try:
            self._get("/configuration")
            return True
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            logger.debug("TMDB connection test failed: %s", exc)
            return False

    def search(
        self,
        title: str,
        *,
        year: int | None = None,
        media_type: str = "movie",
    ) -> TmdbSearchResult | None:
        """Search TMDB for the best matching movie or TV show.

        Returns the first result dict from ``/search/{movie|tv}``, or
        ``None`` on error / no matches. The caller chooses what to do
        with the raw payload (pass it through :meth:`shape_card`, merge
        specific fields, etc.).
        """
        endpoint = "movie" if media_type == "movie" else "tv"
        params: dict[str, object] = {"query": title}
        if year:
            params["year" if endpoint == "movie" else "first_air_date_year"] = year
        try:
            results = self._get(f"/search/{endpoint}", params).get("results") or []
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            self._log_request_failure(f"search/{endpoint}({title!r})", exc)
            return None
        return results[0] if results else None

    def search_multi(self, title: str) -> TmdbSearchResult | None:
        """Search TMDB across movies, TV, and people — returns the raw
        first result.

        Used by callers (e.g. the dashboard deleted-items panel) that
        only know the title and don't care about media_type. The first
        result may be a ``person`` entry with no poster — the caller
        is responsible for checking ``poster_path``.
        """
        try:
            results = self._get("/search/multi", {"query": title}).get("results") or []
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            self._log_request_failure(f"search/multi({title!r})", exc)
            return None
        return results[0] if results else None

    def _get_results(
        self, path: str, *, params: dict[str, object], label: str
    ) -> list[dict[str, object]]:
        """Fetch *path* and return its ``results`` list, ``[]`` on any failure.

        Four collection-endpoint methods previously duplicated the same
        try/except/.get("results") block (search_multi_paged, trending,
        popular_movies, popular_tv).  Returns an empty list on transport
        failure, HTTP error, JSON decode failure, or a payload that isn't
        a dict — never raises.
        """
        try:
            payload = self._get(path, params)
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            self._log_request_failure(label, exc)
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        return results if isinstance(results, list) else []

    def search_multi_paged(self, query: str, page: int = 1) -> list[dict[str, object]]:
        """Return one page of ``/search/multi`` results as a raw list.

        Unlike :meth:`search_multi` (which returns only the first hit),
        this returns the full page so callers can merge multiple pages
        themselves. Returns an empty list on error or when TMDB returns
        no hits — never raises.
        """
        return self._get_results(
            "/search/multi",
            params={"query": query, "include_adult": False, "page": page},
            label=f"search/multi paged({query!r}, page={page})",
        )

    def trending(self, page: int = 1) -> list[dict[str, object]]:
        """Return one page of ``/trending/all/week`` results.

        TMDB recomputes this daily so the caller may cache aggressively.
        Returns an empty list on error — never raises.
        """
        return self._get_results(
            "/trending/all/week",
            params={"page": page},
            label=f"trending(page={page})",
        )

    def popular_movies(self, page: int = 1) -> list[dict[str, object]]:
        """Return one page of ``/movie/popular`` results.

        Items do not include a ``media_type`` field — callers that need
        it must inject ``"movie"`` themselves.
        Returns an empty list on error — never raises.
        """
        return self._get_results(
            "/movie/popular",
            params={"page": page},
            label=f"movie/popular(page={page})",
        )

    def popular_tv(self, page: int = 1) -> list[dict[str, object]]:
        """Return one page of ``/tv/popular`` results.

        Items do not include a ``media_type`` field — callers that need
        it must inject ``"tv"`` themselves.
        Returns an empty list on error — never raises.
        """
        return self._get_results(
            "/tv/popular",
            params={"page": page},
            label=f"tv/popular(page={page})",
        )

    def details(self, media_type: str, tmdb_id: int) -> TmdbDetailsPayload | None:
        """Return the raw TMDB details payload with videos + credits appended.

        On HTTP or JSON errors returns ``None`` — callers should skip
        enrichment rather than raise.
        """
        endpoint = "movie" if media_type == "movie" else "tv"
        try:
            result: object = self._get(
                f"/{endpoint}/{tmdb_id}",
                {"append_to_response": "videos,credits"},
            )
            return result if isinstance(result, dict) else None  # type: ignore[return-value]
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            self._log_request_failure(f"{endpoint}/{tmdb_id} details", exc)
            return None

    # ------------------------------------------------------------------
    # Shaping helpers — pure functions, no side effects
    # ------------------------------------------------------------------
    #
    # The implementations live in :mod:`._tmdb_shapes`; they are re-bound
    # here as static methods so the historical ``TmdbClient.shape_card`` /
    # ``TmdbClient.shape_detail`` call sites (and the test monkeypatches
    # that target them) keep working unchanged.
    shape_card = staticmethod(shape_card)
    shape_detail = staticmethod(shape_detail)
