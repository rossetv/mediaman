"""Shared HTTP base for Arr-family API clients (Radarr, Sonarr, etc.).

``ArrClient`` holds the constructor and the four methods that are identical
across every *arr service so that subclasses only contain service-specific
logic. All outbound calls route through :class:`SafeHTTPClient` for SSRF
re-validation, size capping, redirect refusal, and retry/backoff on
transient errors (429/502/503/504 on GETs; see :class:`SafeHTTPClient`).

The client also surfaces the last fetch error via :attr:`last_error` so UI
layers can display a banner instead of silently showing a stale queue.
"""

from __future__ import annotations

from typing import Any, cast

import requests
from requests import RequestException

from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError

#: Split timeout: 5 s to establish a TCP connection, 30 s to read the body.
#: Radarr/Sonarr responses are usually under 1 s on the LAN; the 30 s read
#: budget covers the rare case of a large library dump (tens of thousands of
#: items) on a slow NAS.
_ARR_TIMEOUT: tuple[float, float] = (5.0, 30.0)


class ArrClient:
    """Base class for *arr API clients.

    Provides authenticated HTTP helpers and a connection test.  Subclasses
    must not override ``__init__`` — they receive ``url`` and ``api_key``
    here and should add no extra constructor arguments.

    :attr:`last_error` is ``None`` when the last call succeeded and is set
    to the exception string on failure.  Callers that want to surface fetch
    errors to the UI should read this attribute after calling any method.
    """

    def __init__(self, url: str, api_key: str):
        self._url = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}
        self._session = requests.Session()
        self._http = SafeHTTPClient(
            self._url,
            session=self._session,
            default_timeout=_ARR_TIMEOUT,
        )
        #: Set to the error string of the last failed call; ``None`` on success.
        self.last_error: str | None = None

    def _get(self, path: str) -> dict | list:
        """Perform an authenticated GET.  Sets :attr:`last_error` on failure.

        Raises:
            ValueError: If the response body is null (empty or explicitly null JSON).
        """
        try:
            resp = self._http.get(path, headers=self._headers)
            self.last_error = None
            result = resp.json()
            if result is None:
                raise ValueError(f"Arr returned null for {path}")
            return result
        except Exception as exc:
            self.last_error = str(exc)
            raise

    def _put(self, path: str, data: dict) -> None:
        """Perform an authenticated PUT.  Sets :attr:`last_error` on failure."""
        try:
            self._http.put(path, headers=self._headers, json=data)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            raise

    def _post(self, path: str, data: dict) -> dict | list:
        """Perform an authenticated POST.  Sets :attr:`last_error` on failure."""
        try:
            resp = self._http.post(path, headers=self._headers, json=data)
            self.last_error = None
            return resp.json()
        except Exception as exc:
            self.last_error = str(exc)
            raise

    def _delete(self, path: str) -> None:
        """Perform an authenticated DELETE.  Sets :attr:`last_error` on failure."""
        try:
            self._http.delete(path, headers=self._headers)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            raise

    def lookup_by_tmdb_id(self, tmdb_id: int, *, endpoint: str) -> list[dict[str, Any]]:
        """Return the lookup results for a given TMDB ID.

        ``endpoint`` is the Arr-specific lookup path, e.g.
        ``"/api/v3/movie/lookup"`` or ``"/api/v3/series/lookup"``.
        The query term ``tmdb:<id>`` is appended automatically.
        """
        result = self._get(f"{endpoint}?term=tmdb:{tmdb_id}") or []
        assert isinstance(result, list)
        return cast(list[dict[str, Any]], result)

    def lookup_by_tvdb_id(self, tvdb_id: int, *, endpoint: str) -> list[dict[str, Any]]:
        """Return the lookup results for a given TVDB ID.

        ``endpoint`` is the Arr-specific lookup path, e.g.
        ``"/api/v3/series/lookup"``.
        The query term ``tvdb:<id>`` is appended automatically.
        """
        result = self._get(f"{endpoint}?term=tvdb:{tvdb_id}") or []
        assert isinstance(result, list)
        return cast(list[dict[str, Any]], result)

    def lookup_by_imdb_id(self, imdb_id: str, *, endpoint: str) -> list[dict[str, Any]]:
        """Return the lookup results for a given IMDb ID.

        ``endpoint`` is the Arr-specific lookup path.  The query term
        ``imdb:<id>`` is appended automatically.
        """
        result = self._get(f"{endpoint}?term=imdb:{imdb_id}") or []
        assert isinstance(result, list)
        return cast(list[dict[str, Any]], result)

    def lookup_by_term(self, term: str, *, endpoint: str) -> list[dict[str, Any]]:
        """Return lookup results for a free-text search term.

        ``term`` must already be URL-encoded by the caller if it contains
        spaces or special characters.
        """
        result = self._get(f"{endpoint}?term={term}") or []
        assert isinstance(result, list)
        return cast(list[dict[str, Any]], result)

    def get_release(self, item_id: int, *, endpoint: str) -> dict | None:
        """Return a single Arr item by its internal numeric ID.

        Returns ``None`` when the item does not exist (404) or on a network
        error (:exc:`~requests.RequestException`).  All other exceptions —
        including programming errors — are allowed to propagate so they are
        not silently swallowed.
        """
        try:
            result = self._get(f"{endpoint}/{item_id}")
            return result if isinstance(result, dict) else None
        except SafeHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        except RequestException:
            return None

    def test_connection(self) -> bool:
        """Return True if the service's /api/v3/system/status endpoint responds.

        Catches :exc:`SafeHTTPError` (non-2xx responses) and
        :exc:`~requests.RequestException` (network/transport errors) only —
        not the broad ``Exception`` which would swallow ``SystemExit``,
        ``KeyboardInterrupt``, and programming errors.
        """
        try:
            self._get("/api/v3/system/status")
            return True
        except (SafeHTTPError, RequestException):
            return False
