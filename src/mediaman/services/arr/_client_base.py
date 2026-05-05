"""Raw HTTP base class and shared plumbing for *arr-family API clients.

This module is an internal implementation detail of the ``arr`` package.
Public consumers should import from :mod:`mediaman.services.arr.base`.

Extracted from ``base.py`` so the plumbing layer can be tested and evolved
without touching the spec-driven :class:`~mediaman.services.arr.base.ArrClient`
that lives alongside it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, cast

import requests
from requests import RequestException

from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError

#: Split timeout: 5 s to establish a TCP connection, 30 s to read the body.
#: Radarr/Sonarr responses are usually under 1 s on the LAN; the 30 s read
#: budget covers the rare case of a large library dump (tens of thousands of
#: items) on a slow NAS.
_ARR_TIMEOUT: tuple[float, float] = (5.0, 30.0)

logger = logging.getLogger("mediaman")


class ArrKindMismatch(RuntimeError):
    """Raised when a kind-specific method is called on the wrong :class:`~mediaman.services.arr.base.ArrClient` variant.

    For example, calling :meth:`~mediaman.services.arr.base.ArrClient.delete_episode_files` on a client
    built with :data:`~mediaman.services.arr.spec.RADARR_SPEC`
    (``kind="movie"``) raises this exception.
    """


class _ArrClientBase:
    """Raw HTTP helpers and shared plumbing for *arr API clients.

    Not intended for direct instantiation — use
    :class:`~mediaman.services.arr.base.ArrClient` instead.

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

    # ---------------------------------------------------------------------
    # Add-flow helpers (root folder / quality profile pickers)
    # ---------------------------------------------------------------------
    #
    # Both Radarr and Sonarr require the caller to nominate a root folder and
    # a quality profile when adding a new release. Three near-identical copies
    # of "GET /rootfolder, take [0], else fall back to '/tv' or '/movies'"
    # used to live in the per-client modules; the same was true of the
    # hardcoded ``quality_profile_id=4`` default. The helpers below replace
    # those copies so every add path uses the same logic and the same set of
    # error messages.
    #
    # The picked values are cached on the instance because every add-flow
    # touches them and the underlying lists barely change at runtime.

    _root_folder_cache: str | None = None
    _quality_profile_cache: int | None = None

    def _choose_root_folder(self) -> str:
        """Return the path of the first configured root folder.

        Cached on the client instance so a burst of adds in a single
        process pays one API call. Raises :exc:`RuntimeError` when the
        Arr service has no root folders configured — the previous default
        of ``"/tv"`` / ``"/movies"`` paved over a common misconfiguration
        and led to silent failures further down the pipeline.
        """
        if self._root_folder_cache is not None:
            return self._root_folder_cache
        result = self._get("/api/v3/rootfolder")
        root_folders = cast(list[dict[str, Any]], result) if isinstance(result, list) else []
        if not root_folders:
            raise RuntimeError(
                f"{type(self).__name__}: no root folders configured — "
                "set one in the service's UI before adding releases"
            )
        path = root_folders[0].get("path")
        if not isinstance(path, str) or not path:
            raise RuntimeError(
                f"{type(self).__name__}: first root folder has no 'path' — "
                "the service's response is malformed"
            )
        self._root_folder_cache = path
        return path

    def _choose_quality_profile(self) -> int:
        """Return the id of the lowest-numbered quality profile.

        Used by the add-flow when the caller doesn't pin a specific
        profile. Cached on the client instance. Raises
        :exc:`RuntimeError` when the Arr service has no quality profiles
        configured (which would otherwise have silently picked id ``4``
        whether or not such a profile exists).
        """
        if self._quality_profile_cache is not None:
            return self._quality_profile_cache
        result = self._get("/api/v3/qualityprofile")
        profiles = cast(list[dict[str, Any]], result) if isinstance(result, list) else []
        ids = [int(p["id"]) for p in profiles if isinstance(p.get("id"), int)]
        if not ids:
            raise RuntimeError(
                f"{type(self).__name__}: no quality profiles configured — "
                "set one in the service's UI before adding releases"
            )
        chosen = min(ids)
        self._quality_profile_cache = chosen
        return chosen

    # ---------------------------------------------------------------------
    # Shared read-modify-write retry helper
    # ---------------------------------------------------------------------

    def _unmonitor_with_retry(
        self,
        *,
        fetch_entity: Callable[[], dict],
        put_url: str,
        is_already_unmonitored: Callable[[dict], bool],
        apply_unmonitor: Callable[[dict], None],
        log_prefix: str,
        log_id: str,
        max_retries: int = 3,
    ) -> None:
        """Perform an optimistic-concurrency read-modify-write to set monitored=False.

        Both :meth:`~mediaman.services.arr.base.ArrClient.unmonitor_season` and
        :meth:`~mediaman.services.arr.base.ArrClient.unmonitor_movie` implement
        the same retry loop; this helper encapsulates the shared pattern.

        Args:
            fetch_entity: Callable that returns a fresh entity payload (dict).
            put_url: PUT path for the entity, e.g. ``"/api/v3/movie/{id}"``.
            is_already_unmonitored: Callable that inspects the entity and
                returns ``True`` when monitored is already ``False``.
            apply_unmonitor: Callable that mutates the entity dict in-place
                to set the monitored flag to ``False``.
            log_prefix: Prefix string for log messages, e.g.
                ``"radarr.unmonitor_movie"`` or ``"sonarr.unmonitor_season"``.
                Kept distinct per call site so log lines remain grep-searchable.
            log_id: Identifier fragment for log messages, e.g.
                ``f"movie_id={movie_id}"`` or
                ``f"series_id={series_id} season={season_number}"``.
                Appended to ``log_prefix`` in each emitted message.
            max_retries: Maximum number of read-modify-write attempts before
                raising :exc:`RuntimeError`.
        """
        last_observed: bool | None = None
        for attempt in range(max_retries):
            entity = fetch_entity()
            if is_already_unmonitored(entity):
                # Already unmonitored — either nothing to do (first
                # attempt) or another writer beat us to it (subsequent
                # attempts). Either way, the desired state is achieved.
                if last_observed is True:
                    logger.warning(
                        "%s: concurrent writer set monitored=False "
                        "on %s while we were retrying — exiting cleanly",
                        log_prefix,
                        log_id,
                    )
                return
            apply_unmonitor(entity)
            logger.debug(
                "%s: issuing full-payload PUT for %s "
                "(attempt %d) — a concurrent write to this record would "
                "be silently overwritten",
                log_prefix,
                log_id,
                attempt + 1,
            )
            try:
                self._put(put_url, cast(dict, entity))
                return
            except Exception:
                if attempt + 1 >= max_retries:
                    raise
                logger.warning(
                    "%s: PUT failed for %s "
                    "(attempt %d/%d) — re-reading and retrying",
                    log_prefix,
                    log_id,
                    attempt + 1,
                    max_retries,
                )
                last_observed = True
        raise RuntimeError(
            f"{log_prefix}: gave up after {max_retries} retries for "
            f"{log_id} — concurrent writes kept interleaving"
        )
