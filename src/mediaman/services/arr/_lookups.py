"""Shared lookup + reachability helpers for *arr clients.

The lookup methods are the same across Sonarr and Radarr — only the
``endpoint`` differs — so they live here as a mixin rather than being
duplicated per flavour.
"""

from __future__ import annotations

from typing import cast

from requests import RequestException

from mediaman.services.arr._types import ArrLookupResult
from mediaman.services.infra import SafeHTTPError


class _LookupsMixin:
    """Lookup-by-id and reachability helpers shared by Sonarr and Radarr.

    Each helper takes an explicit *endpoint* path so the caller selects
    ``/api/v3/series/lookup`` or ``/api/v3/movie/lookup`` rather than the
    helper inferring it from the client kind.
    """

    def lookup_by_tmdb_id(self, tmdb_id: int, *, endpoint: str) -> list[ArrLookupResult]:
        """Return the lookup results for a given TMDB ID."""
        result = self._get(f"{endpoint}?term=tmdb:{tmdb_id}") or []  # type: ignore[attr-defined]
        return cast(list[ArrLookupResult], result)

    def lookup_by_tvdb_id(self, tvdb_id: int, *, endpoint: str) -> list[ArrLookupResult]:
        """Return the lookup results for a given TVDB ID."""
        result = self._get(f"{endpoint}?term=tvdb:{tvdb_id}") or []  # type: ignore[attr-defined]
        return cast(list[ArrLookupResult], result)

    def lookup_by_imdb_id(self, imdb_id: str, *, endpoint: str) -> list[ArrLookupResult]:
        """Return the lookup results for a given IMDb ID."""
        result = self._get(f"{endpoint}?term=imdb:{imdb_id}") or []  # type: ignore[attr-defined]
        return cast(list[ArrLookupResult], result)

    def lookup_by_term(self, term: str, *, endpoint: str) -> list[ArrLookupResult]:
        """Return lookup results for a free-text search term.

        *term* must already be URL-encoded by the caller if it contains
        spaces or special characters.
        """
        result = self._get(f"{endpoint}?term={term}") or []  # type: ignore[attr-defined]
        return cast(list[ArrLookupResult], result)

    def get_release(self, item_id: int, *, endpoint: str) -> dict | None:
        """Return a single Arr item by its internal numeric ID.

        Returns ``None`` when the item does not exist (404) or on a
        network error.  All other exceptions — including programming
        errors — are allowed to propagate.
        """
        try:
            result = self._get(f"{endpoint}/{item_id}")  # type: ignore[attr-defined]
            return result if isinstance(result, dict) else None
        except SafeHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        except RequestException:
            return None

    def is_reachable(self) -> bool:
        """Return True if ``/api/v3/system/status`` responds.

        Catches :exc:`SafeHTTPError` (non-2xx responses) and
        :exc:`~requests.RequestException` (network/transport errors)
        only — not the broad ``Exception`` which would swallow
        ``SystemExit``, ``KeyboardInterrupt``, and programming errors.
        """
        try:
            self._get("/api/v3/system/status")  # type: ignore[attr-defined]
            return True
        except (SafeHTTPError, RequestException):
            return False
