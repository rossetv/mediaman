"""Unified TMDB client: search, details, and shape helpers.

Callers read the bearer token through get_string_setting so token decryption stays
consistent. The shape TypedDicts and helpers live in _tmdb_shapes and are re-exported
here; TmdbClient re-binds the helpers as static methods so callers can reach them
through the client.
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
    certificate,
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

# Module-level client cache.
#
# Building a ``TmdbClient`` per request would create a brand-new
# ``requests.Session`` each time, paying DNS + TLS handshake costs against
# ``api.themoviedb.org`` instead of reusing the connection pool.  We cache one
# client per (token, timeout) pair so multiple callers share the same session.
#
# Max size: 4 entries — one per timeout variant with headroom for token
# rotation (the old token's entry ages out as soon as the new one is used).
# Beyond 4 entries the oldest is evicted so the cache cannot grow without
# bound if callers vary ``timeout`` unexpectedly.  Rebuilt on process restart.
_CLIENT_CACHE: dict[tuple[str, float], TmdbClient] = {}
_CLIENT_CACHE_LOCK = threading.Lock()
_CLIENT_CACHE_MAXSIZE = 4


# TMDB attribution notice
# TMDB requires that all products using their API display the TMDB logo
# and a "This product uses the TMDB API but is not endorsed or certified
# by TMDB" attribution notice. Poster image requests to image.tmdb.org
# are standard CDN fetches — they are not tracking pixels. No personal
# data is embedded in the URLs generated here. See the TMDB API Terms of
# Use at https://www.themoviedb.org/terms-of-use for the full attribution
# requirements.


class TmdbClient:
    """Thin wrapper around api.themoviedb.org/3 using a Bearer read-token from settings
    (key 'tmdb_read_token', may be encrypted).

    from_db returns None when no token is configured or decryption fails.
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

    @classmethod
    def from_db(
        cls,
        conn: sqlite3.Connection,
        secret_key: str,
        *,
        timeout: float = 10.0,
    ) -> TmdbClient | None:
        """Build a client from the settings table, or None when the token is missing or
        cannot be decrypted; callers must handle None.

        Clients are cached per (token, timeout) so the requests.Session and its TLS pool
        are shared; rotating the token builds a fresh client.
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
            # Evict the insertion-order-oldest entry when the cache is full
            # so it cannot grow without bound if callers vary ``timeout``.
            # This is FIFO, not true LRU — cache hits do not reorder the
            # dict, so the entry evicted is the one inserted longest ago,
            # not the least-recently accessed. With _CLIENT_CACHE_MAXSIZE=4
            # and a single token in practice, this is indistinguishable
            # from LRU; the comment is correct about the mechanism used.
            if len(_CLIENT_CACHE) >= _CLIENT_CACHE_MAXSIZE:
                oldest_key = next(iter(_CLIENT_CACHE))
                del _CLIENT_CACHE[oldest_key]
            _CLIENT_CACHE[cache_key] = client
            return client

    # rationale: returns resp.json() whose runtime type varies per endpoint
    # (dict for single-item endpoints, list for collection endpoints). Each
    # public method narrows the result via .get() or isinstance guards and
    # returns a fully-typed value; annotating _get as Any avoids cascading
    # casts across every caller while keeping all public API return types
    # concrete.
    def _get(self, path: str, params: dict[str, object] | None = None) -> Any:
        """GET *path* with authentication and return the parsed JSON.

        Exceptions propagate; each public method decides whether to return None or [].
        Raises ValueError on a non-JSON body (e.g. an HTML error page during an outage).
        It is not a RequestException, so callers must catch it explicitly.
        """
        return self._http.get(path, headers=self._headers, params=params or {}).json()

    @staticmethod
    def _log_request_failure(label: str, exc: Exception) -> None:
        """Log a TMDB request failure by severity.

        401/403 is WARNING (operator-actionable token), 5xx is ERROR (TMDB outage),
        anything else is DEBUG. Callers return None or [] regardless.
        """
        if isinstance(exc, SafeHTTPError):
            if exc.status_code in (401, 403):
                logger.warning(
                    "tmdb.auth_failure",
                    extra={"label": label, "status_code": exc.status_code},
                )
                return
            if 500 <= exc.status_code < 600:
                logger.error(
                    "tmdb.request_failed",
                    extra={"label": label, "status_code": exc.status_code},
                    exc_info=True,
                )
                return
        logger.debug("tmdb.request_failed", extra={"label": label, "exc": str(exc)})

    def is_reachable(self) -> bool:
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
        """Return the first result of /search/{movie|tv}, or None on error or no match."""
        try:
            return self.search_or_raise(title, year=year, media_type=media_type)
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            endpoint = "movie" if media_type == "movie" else "tv"
            self._log_request_failure(f"search/{endpoint}({title!r})", exc)
            return None

    def search_or_raise(
        self,
        title: str,
        *,
        year: int | None = None,
        media_type: str = "movie",
    ) -> TmdbSearchResult | None:
        """Return the first result of /search/{movie|tv}, or None when TMDB has no match.

        A failed request raises (SafeHTTPError, requests.RequestException or ValueError),
        so a caller can tell a no-match from an outage.
        """
        endpoint = "movie" if media_type == "movie" else "tv"
        params: dict[str, object] = {"query": title}
        if year:
            params["year" if endpoint == "movie" else "first_air_date_year"] = year
        results = self._get(f"/search/{endpoint}", params).get("results") or []
        return results[0] if results else None

    def search_multi(self, title: str) -> TmdbSearchResult | None:
        """Return the first /search/multi result, for callers that only know the title.

        The result may be a person entry with no poster; the caller must check
        poster_path.
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
        """Fetch *path* and return its results list; [] on any failure (transport, HTTP,
        JSON decode or a non-dict payload). Never raises.
        """
        try:
            payload = self._get(path, params)
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            self._log_request_failure(label, exc)
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        return results if isinstance(results, list) else []

    def search_multi_paged(self, query: str, page: int = 1) -> list[dict[str, object]]:
        """Return one page of /search/multi results; [] on error. Unlike search_multi it
        returns the full page.
        """
        return self._get_results(
            "/search/multi",
            params={"query": query, "include_adult": False, "page": page},
            label=f"search/multi paged({query!r}, page={page})",
        )

    def trending(self, page: int = 1) -> list[dict[str, object]]:
        """Return one page of /trending/all/week results; [] on error. TMDB recomputes this
        daily, so callers may cache.
        """
        return self._get_results(
            "/trending/all/week",
            params={"page": page},
            label=f"trending(page={page})",
        )

    def popular_movies(self, page: int = 1) -> list[dict[str, object]]:
        """Return one page of /movie/popular results; [] on error.

        Items have no media_type; callers inject "movie".
        """
        return self._get_results(
            "/movie/popular",
            params={"page": page},
            label=f"movie/popular(page={page})",
        )

    def popular_tv(self, page: int = 1) -> list[dict[str, object]]:
        """Return one page of /tv/popular results; [] on error.

        Items have no media_type; callers inject "tv".
        """
        return self._get_results(
            "/tv/popular",
            params={"page": page},
            label=f"tv/popular(page={page})",
        )

    def details(self, media_type: str, tmdb_id: int) -> TmdbDetailsPayload | None:
        """Return the raw details payload with videos, credits and the age-rating data
        (release_dates for movies, content_ratings and external_ids for TV) appended, or
        None on error.
        """
        endpoint = "movie" if media_type == "movie" else "tv"
        try:
            result: object = self._get(
                f"/{endpoint}/{tmdb_id}",
                {
                    "append_to_response": "videos,credits,"
                    + ("release_dates" if endpoint == "movie" else "content_ratings,external_ids")
                },
            )
            return result if isinstance(result, dict) else None  # type: ignore[return-value]
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            self._log_request_failure(f"{endpoint}/{tmdb_id} details", exc)
            return None

    def find_by_tvdb(self, tvdb_id: int) -> int | None:
        """Return the TMDB TV id for a TVDB id via /find, or None when TMDB has no match.

        A failed request raises (SafeHTTPError, requests.RequestException or ValueError),
        so a caller can hold a no-match without holding an outage as one.
        """
        try:
            payload = self._get(f"/find/{tvdb_id}", {"external_source": "tvdb_id"})
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            self._log_request_failure(f"find/{tvdb_id}", exc)
            raise
        results = payload.get("tv_results") if isinstance(payload, dict) else None
        if isinstance(results, list) and results and isinstance(results[0], dict):
            tmdb_id = results[0].get("id")
            return tmdb_id if isinstance(tmdb_id, int) else None
        return None

    def discover(self, media_type: str, params: dict[str, object]) -> dict[str, Any]:
        """Return one page of /discover/{movie|tv}, adult titles not asked for. Raises as
        find_by_tvdb does, and ValueError for a payload without a results list."""
        # requests would send a bool as "False".
        payload = self._get(
            f"/discover/{'movie' if media_type == 'movie' else 'tv'}",
            {**params, "include_adult": "false"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("discover answered without results")
        return payload

    def person(self, person_id: int) -> dict[str, Any]:
        """Return a person's raw payload with every film and TV credit appended. Raises as
        find_by_tvdb does, and ValueError for a payload that is no person's."""
        payload = self._get(f"/person/{person_id}", {"append_to_response": "combined_credits"})
        if not isinstance(payload, dict) or not payload.get("name"):
            raise ValueError("person answered without a name")
        return payload

    # The implementations live in _tmdb_shapes; they are re-bound
    # here as static methods so ``TmdbClient.shape_card`` /
    # ``TmdbClient.shape_detail`` call sites (and test monkeypatches
    # that target them) work.
    shape_card = staticmethod(shape_card)
    shape_detail = staticmethod(shape_detail)
    certificate = staticmethod(certificate)
