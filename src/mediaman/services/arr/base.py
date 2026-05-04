"""Shared HTTP base and spec-driven unified client for *arr-family APIs.

This module provides two classes:

* :class:`_ArrClientBase` — raw HTTP helpers (GET/PUT/POST/DELETE), the
  connection test, add-flow pickers (root folder, quality profile), and
  lookup helpers.  It is not instantiated directly; it is the superclass of
  :class:`ArrClient`.

* :class:`ArrClient` — the spec-driven unified client.  It accepts an
  :class:`~mediaman.services.arr.spec.ArrSpec` as its first constructor
  argument, which determines whether it talks to Sonarr (``kind="series"``)
  or Radarr (``kind="movie"``).  All service-specific methods are present on
  this class; kind-specific ones raise :exc:`ArrKindMismatch` when called on
  the wrong variant.

Back-compat subclasses in ``sonarr.py`` and ``radarr.py`` pre-bind the
appropriate spec so existing callers need no changes.

All outbound calls route through :class:`SafeHTTPClient` for SSRF
re-validation, size capping, redirect refusal, and retry/backoff on
transient errors (429/502/503/504 on GETs; see :class:`SafeHTTPClient`).

:attr:`last_error` is ``None`` when the last call succeeded and is set to
the exception string on failure so UI layers can display a banner instead
of silently showing a stale queue.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import requests
from requests import RequestException

from mediaman.services.arr.spec import ArrSpec
from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError

#: Split timeout: 5 s to establish a TCP connection, 30 s to read the body.
#: Radarr/Sonarr responses are usually under 1 s on the LAN; the 30 s read
#: budget covers the rare case of a large library dump (tens of thousands of
#: items) on a slow NAS.
_ARR_TIMEOUT: tuple[float, float] = (5.0, 30.0)

logger = logging.getLogger("mediaman")


class ArrKindMismatch(RuntimeError):
    """Raised when a kind-specific method is called on the wrong :class:`ArrClient` variant.

    For example, calling :meth:`ArrClient.delete_episode_files` on a client
    built with :data:`~mediaman.services.arr.spec.RADARR_SPEC`
    (``kind="movie"``) raises this exception.
    """


class _ArrClientBase:
    """Raw HTTP helpers and shared plumbing for *arr API clients.

    Not intended for direct instantiation — use :class:`ArrClient` instead.

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


class ArrClient(_ArrClientBase):
    """Spec-driven unified client for Sonarr and Radarr v3 APIs.

    Pass a :class:`~mediaman.services.arr.spec.ArrSpec` (typically
    :data:`~mediaman.services.arr.spec.SONARR_SPEC` or
    :data:`~mediaman.services.arr.spec.RADARR_SPEC`) as the first argument.
    The spec determines which service this client speaks to.

    All methods from both Sonarr and Radarr are present on this class.
    Methods that are specific to one service kind raise
    :exc:`ArrKindMismatch` when called on the wrong variant, e.g. calling
    :meth:`delete_episode_files` on a Radarr client.

    Existing callers that import ``SonarrClient`` or ``RadarrClient`` from
    their respective modules continue to work unchanged — those modules
    provide thin back-compat subclasses that pre-bind the appropriate spec.
    """

    def __init__(self, spec: ArrSpec, url: str, api_key: str):
        super().__init__(url, api_key)
        #: The spec that controls this client's service-specific behaviour.
        self.spec = spec

    def _require_series(self, method: str) -> None:
        """Raise :exc:`ArrKindMismatch` if this client is not a Sonarr (series) client."""
        if self.spec.kind != "series":
            raise ArrKindMismatch(
                f"{method} is only available on series (Sonarr) clients; "
                f"this client has kind={self.spec.kind!r}"
            )

    def _require_movie(self, method: str) -> None:
        """Raise :exc:`ArrKindMismatch` if this client is not a Radarr (movie) client."""
        if self.spec.kind != "movie":
            raise ArrKindMismatch(
                f"{method} is only available on movie (Radarr) clients; "
                f"this client has kind={self.spec.kind!r}"
            )

    # ------------------------------------------------------------------
    # Sonarr-specific methods (kind="series")
    # ------------------------------------------------------------------

    def delete_episode_files(self, series_id: int, season_number: int) -> None:
        """Delete all episode files for a season from disk via Sonarr.

        Uses the ``/api/v3/episodefile/bulk`` endpoint (single POST with an
        ``episodeFileIds`` list) rather than N serial DELETEs, which would
        hammer Sonarr on large seasons.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("delete_episode_files")
        efs = cast(list[dict[str, Any]], self._get(f"/api/v3/episodefile?seriesId={series_id}"))
        ids = [
            int(ef["id"])
            for ef in efs
            if ef.get("seasonNumber") == season_number and ef.get("id") is not None
        ]
        if ids:
            self._delete_bulk_episode_files(ids)

    def _delete_bulk_episode_files(self, episode_file_ids: list[int]) -> None:
        """Delete multiple episode files in a single Sonarr API call.

        Calls ``DELETE /api/v3/episodefile/bulk`` with ``{"episodeFileIds": [...]}``
        — supported since Sonarr v3.  Falls back to serial deletes when the
        endpoint returns 404 (Sonarr version too old).
        """
        try:
            self._http.delete(
                "/api/v3/episodefile/bulk",
                headers=self._headers,
                json={"episodeFileIds": episode_file_ids},
            )
        except SafeHTTPError as exc:
            if exc.status_code == 404:
                logger.debug(
                    "sonarr.delete_episode_files: bulk endpoint not available "
                    "(HTTP 404), falling back to serial deletes"
                )
                for ef_id in episode_file_ids:
                    self._delete(f"/api/v3/episodefile/{ef_id}")
            else:
                raise

    def delete_series(self, series_id: int) -> None:
        """Delete a series from Sonarr, its files, and add to exclusion list.

        Defensively coerces ``series_id`` to ``int`` at the boundary even
        though the type hint already forces it — a future caller passing
        a string would otherwise allow URL-extension (e.g. an id like
        ``"1?evil=…"`` would tack arbitrary query parameters onto the
        DELETE).  The cast raises :exc:`ValueError` on malformed input.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("delete_series")
        sid = int(series_id)
        self._delete(f"/api/v3/series/{sid}?deleteFiles=true&addImportListExclusion=true")

    def has_remaining_files(self, series_id: int) -> bool:
        """Return True if the series still has any episode files on disk.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("has_remaining_files")
        efs = cast(list[dict[str, object]], self._get(f"/api/v3/episodefile?seriesId={series_id}"))
        return len(efs) > 0

    def get_series(self) -> list[dict[str, Any]]:
        """Return all series in the Sonarr library.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("get_series")
        data = self._get("/api/v3/series")
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def get_series_by_id(self, series_id: int) -> dict[str, Any]:
        """Return a single series by its Sonarr ID.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        Raises :exc:`ValueError` when Sonarr returns a non-dict response.
        """
        self._require_series("get_series_by_id")
        data = self._get(f"/api/v3/series/{series_id}")
        if not isinstance(data, dict):
            raise ValueError(f"Sonarr returned unexpected response type for series {series_id}")
        return cast(dict[str, Any], data)

    def get_episodes(self, series_id: int) -> list[dict[str, Any]]:
        """Return all episodes for a given series.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("get_episodes")
        data = self._get(f"/api/v3/episode?seriesId={series_id}")
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def get_episode_files(self, series_id: int) -> list[dict[str, Any]]:
        """Return episode file records for a given series.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("get_episode_files")
        data = self._get(f"/api/v3/episodefile?seriesId={series_id}")
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def unmonitor_season(self, series_id: int, season_number: int, *, max_retries: int = 3) -> None:
        """Set ``monitored=False`` for *season_number* of *series_id* in Sonarr.

        Raises :exc:`ValueError` if the series payload contains no seasons
        list, or if the targeted season cannot be located.

        Sonarr v3 has no PATCH endpoint, so this is a read-modify-write on
        the full series payload.  To narrow the TOCTOU window, the read is
        retried up to ``max_retries`` times: if the targeted season's
        ``monitored`` flag changed between the GET and a re-GET (i.e.
        another writer beat us to it), the round is restarted from a
        fresh GET.  After ``max_retries`` failed rounds the call raises
        :exc:`RuntimeError` rather than silently clobbering the foreign
        write — losing data is worse than failing loudly.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("unmonitor_season")
        last_observed: bool | None = None
        for attempt in range(max_retries):
            series = self.get_series_by_id(series_id)
            seasons = series.get("seasons")
            if not isinstance(seasons, list):
                raise ValueError(f"Sonarr series {series_id} has no 'seasons' list")
            target = next((s for s in seasons if s.get("seasonNumber") == season_number), None)
            if target is None:
                raise ValueError(f"Sonarr series {series_id} has no season {season_number}")
            current_monitored = bool(target.get("monitored", False))
            if not current_monitored:
                # Already unmonitored — either nothing to do (first
                # attempt) or another writer beat us to it (subsequent
                # attempts). Either way, the desired state is achieved.
                if last_observed is True:
                    logger.warning(
                        "sonarr.unmonitor_season: concurrent writer set monitored=False "
                        "on series_id=%s season=%s while we were retrying — exiting cleanly",
                        series_id,
                        season_number,
                    )
                return
            target["monitored"] = False
            logger.debug(
                "sonarr.unmonitor_season: issuing full-payload PUT for series_id=%s "
                "season=%s (attempt %d) — a concurrent write to this series would "
                "be silently overwritten",
                series_id,
                season_number,
                attempt + 1,
            )
            try:
                self._put(f"/api/v3/series/{series_id}", cast(dict, series))
                return
            except Exception:
                if attempt + 1 >= max_retries:
                    raise
                logger.warning(
                    "sonarr.unmonitor_season: PUT failed for series_id=%s season=%s "
                    "(attempt %d/%d) — re-reading and retrying",
                    series_id,
                    season_number,
                    attempt + 1,
                    max_retries,
                )
                last_observed = current_monitored
        raise RuntimeError(
            f"sonarr.unmonitor_season: gave up after {max_retries} retries for "
            f"series_id={series_id} season={season_number} — concurrent writes kept "
            "interleaving"
        )

    def remonitor_season(self, series_id: int, season_number: int) -> None:
        """Set ``monitored=True`` for *season_number* of *series_id* and trigger a search.

        Raises:
            ValueError: If the series payload contains no seasons list.
            ArrKindMismatch: If called on a Radarr client.
        """
        self._require_series("remonitor_season")
        series = self.get_series_by_id(series_id)
        seasons = series.get("seasons")
        if not isinstance(seasons, list):
            raise ValueError(f"Sonarr series {series_id} has no 'seasons' list")
        for season in seasons:
            if season["seasonNumber"] == season_number:
                season["monitored"] = True
        self._put(f"/api/v3/series/{series_id}", cast(dict, series))
        self._post(
            "/api/v3/command",
            {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number},
        )

    def search_series(self, series_id: int) -> None:
        """Trigger a Sonarr SeriesSearch command for a single series.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("search_series")
        self._post("/api/v3/command", {"name": "SeriesSearch", "seriesId": series_id})

    def get_missing_series(self) -> dict[int, str]:
        """Return ``{series_id: series_title}`` for every series with at least
        one monitored missing episode, matching Sonarr's Wanted/Missing view.

        Series with zero episode files are included here too — callers that
        already handle the zero-file case should dedupe by ``series_id``.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("get_missing_series")
        out: dict[int, str] = {}
        page = 1
        page_size = 250
        for _ in range(100):  # hard cap to prevent runaway paging
            data = self._get(
                f"/api/v3/wanted/missing?page={page}&pageSize={page_size}"
                "&includeSeries=true&monitored=true"
            )
            if not isinstance(data, dict):
                break
            records = data.get("records") or []
            if not records:
                break
            for rec in records:
                series = rec.get("series") or {}
                sid = series.get("id")
                title = series.get("title", "")
                if sid and sid not in out:
                    out[sid] = title
            total = data.get("totalRecords") or 0
            if page * page_size >= total:
                break
            page += 1
        return out

    def add_series(
        self, tvdb_id: int, title: str, quality_profile_id: int | None = None
    ) -> dict[str, object]:
        """Add a TV series by TVDB ID and trigger a search.

        ``quality_profile_id`` is selected via :meth:`_choose_quality_profile`
        when not specified.  Pass ``tvdb_id <= 0`` and the call raises
        :exc:`ValueError` rather than letting Sonarr return an opaque 400.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("add_series")
        if tvdb_id <= 0:
            raise ValueError(f"tvdb_id must be positive, got {tvdb_id!r}")
        root_path = self._choose_root_folder()
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()
        )

        series_data = {
            "tvdbId": tvdb_id,
            "title": title,
            "qualityProfileId": profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {"searchForMissingEpisodes": True},
        }
        return cast(dict[str, object], self._post("/api/v3/series", series_data))

    def add_series_with_seasons(
        self,
        tvdb_id: int,
        title: str,
        monitored_seasons: list[int],
        search_seasons: list[int],
        quality_profile_id: int | None = None,
    ) -> dict[str, object]:
        """Add a series with an explicit per-season monitor/search plan.

        Seasons listed in ``monitored_seasons`` are added with
        ``monitored=True``; every other season (including season 0)
        is added with ``monitored=False``. The full-series auto-search
        is suppressed; instead a ``SeasonSearch`` command is issued
        for each season number in ``search_seasons``.

        ``quality_profile_id`` is selected via :meth:`_choose_quality_profile`
        when not specified.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("add_series_with_seasons")
        if tvdb_id <= 0:
            raise ValueError(f"tvdb_id must be positive, got {tvdb_id!r}")
        root_path = self._choose_root_folder()
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()
        )

        lookup = cast(list[dict[str, Any]], self._get(f"/api/v3/series/lookup?term=tvdb:{tvdb_id}"))
        if not lookup:
            raise RuntimeError(f"Sonarr lookup returned no results for tvdb:{tvdb_id}")
        meta = lookup[0]

        monitored_set = set(monitored_seasons)
        seasons_payload = [
            {
                "seasonNumber": s["seasonNumber"],
                "monitored": s["seasonNumber"] in monitored_set,
            }
            for s in meta.get("seasons", [])
        ]

        body = {
            "tvdbId": tvdb_id,
            "title": title,
            "qualityProfileId": profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "seasonFolder": True,
            "seasons": seasons_payload,
            "addOptions": {
                "searchForMissingEpisodes": False,
                "monitor": "none",
            },
        }
        new_series = cast(dict[str, object], self._post("/api/v3/series", body))
        series_id = new_series.get("id")

        for season_number in search_seasons:
            self._post(
                "/api/v3/command",
                {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number},
            )

        return new_series

    def get_queue(self) -> list[dict[str, Any]]:
        """Return the current download queue.

        Paginates through all pages — otherwise long queues get silently
        truncated at the default page size, orphaning every NZB whose queue
        record sits past the first page.

        The queue endpoint URL differs between Sonarr and Radarr:
        Sonarr includes ``includeSeries=true&includeEpisode=true``;
        Radarr includes ``includeMovie=true``.
        """
        out: list[dict[str, Any]] = []
        page = 1
        page_size = 500
        if self.spec.kind == "series":
            extra = "&includeSeries=true&includeEpisode=true"
        else:
            extra = "&includeMovie=true"
        for _ in range(20):  # hard cap to prevent runaway paging
            data = self._get(f"/api/v3/queue?page={page}&pageSize={page_size}{extra}")
            if not isinstance(data, dict):
                break
            records = data.get("records") or []
            if not records:
                break
            out.extend(records)
            total = data.get("totalRecords") or 0
            if page * page_size >= total:
                break
            page += 1
        return out

    def lookup_series_by_tmdb(self, tmdb_id: int) -> dict[str, object] | None:
        """Look up a series by TMDB ID via Sonarr's lookup endpoint.

        Returns the first match or ``None`` if the upstream API returns an
        empty list (series genuinely not found in Sonarr's metadata provider).

        Network failures (:exc:`~mediaman.services.infra.http_client.SafeHTTPError`,
        :exc:`~requests.RequestException`) are allowed to propagate so callers
        can distinguish "not found" from "call failed".

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("lookup_series_by_tmdb")
        results = self.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Radarr-specific methods (kind="movie")
    # ------------------------------------------------------------------

    def get_movies(self) -> list[dict[str, Any]]:
        """Return all movies in the Radarr library.

        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("get_movies")
        data = self._get("/api/v3/movie")
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def get_movie_by_id(self, movie_id: int) -> dict[str, object]:
        """Return a single movie by its Radarr ID.

        Raises :exc:`ValueError` when Radarr returns a non-dict response.
        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("get_movie_by_id")
        data = self._get(f"/api/v3/movie/{movie_id}")
        if not isinstance(data, dict):
            raise ValueError(
                f"Radarr returned unexpected type for movie {movie_id}: {type(data).__name__}"
            )
        return data

    def delete_movie(self, movie_id: int) -> None:
        """Delete a movie from Radarr and its files from disk.

        Defensively coerces ``movie_id`` to ``int`` at the boundary so a
        malformed caller (e.g. one passing ``"1?evil=…"`` as a string)
        cannot tack arbitrary query parameters onto the DELETE.

        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("delete_movie")
        mid = int(movie_id)
        self._delete(f"/api/v3/movie/{mid}?deleteFiles=true&addImportExclusion=true")

    def unmonitor_movie(self, movie_id: int, *, max_retries: int = 3) -> None:
        """Set ``monitored=False`` for *movie_id* in Radarr.

        Radarr v3 has no PATCH endpoint, so this is a read-modify-write
        on the full movie payload.  To narrow the TOCTOU window, the read
        is retried up to ``max_retries`` times: if the ``monitored`` flag
        flipped between the GET and a re-GET (i.e. another writer beat us
        to it), the round is restarted from a fresh GET.  After
        ``max_retries`` failed rounds the call raises :exc:`RuntimeError`
        rather than silently clobbering the foreign write.

        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("unmonitor_movie")
        last_observed: bool | None = None
        for attempt in range(max_retries):
            movie = self.get_movie_by_id(movie_id)
            current_monitored = bool(movie.get("monitored", False))
            if not current_monitored:
                # Already unmonitored — either nothing to do (first
                # attempt) or another writer beat us to it (subsequent
                # attempts). Either way, the desired state is achieved.
                if last_observed is True:
                    logger.warning(
                        "radarr.unmonitor_movie: concurrent writer set monitored=False "
                        "on movie_id=%s while we were retrying — exiting cleanly",
                        movie_id,
                    )
                return
            movie["monitored"] = False
            logger.debug(
                "radarr.unmonitor_movie: issuing full-payload PUT for movie_id=%s "
                "(attempt %d) — a concurrent write to this record would be "
                "silently overwritten",
                movie_id,
                attempt + 1,
            )
            try:
                self._put(f"/api/v3/movie/{movie_id}", cast(dict, movie))
                return
            except Exception:
                if attempt + 1 >= max_retries:
                    raise
                logger.warning(
                    "radarr.unmonitor_movie: PUT failed for movie_id=%s "
                    "(attempt %d/%d) — re-reading and retrying",
                    movie_id,
                    attempt + 1,
                    max_retries,
                )
                last_observed = current_monitored
        raise RuntimeError(
            f"radarr.unmonitor_movie: gave up after {max_retries} retries for "
            f"movie_id={movie_id} — concurrent writes kept interleaving"
        )

    def remonitor_movie(self, movie_id: int) -> None:
        """Set ``monitored=True`` for *movie_id* and trigger a search.

        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("remonitor_movie")
        movie = self.get_movie_by_id(movie_id)
        movie["monitored"] = True
        self._put(f"/api/v3/movie/{movie_id}", cast(dict, movie))
        self.search_movie(movie_id)

    def search_movie(self, movie_id: int) -> None:
        """Trigger a Radarr MoviesSearch command for a single movie.

        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("search_movie")
        self._post("/api/v3/command", {"name": "MoviesSearch", "movieIds": [movie_id]})

    def add_movie(
        self, tmdb_id: int, title: str, quality_profile_id: int | None = None
    ) -> dict[str, object]:
        """Add a movie by TMDB ID and trigger a search.

        ``quality_profile_id`` is selected via :meth:`_choose_quality_profile`
        when not specified.  Pass ``tmdb_id <= 0`` and the call raises
        :exc:`ValueError` rather than letting Radarr return an opaque 400.

        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("add_movie")
        if tmdb_id <= 0:
            raise ValueError(f"tmdb_id must be positive, got {tmdb_id!r}")
        root_path = self._choose_root_folder()
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()
        )
        movie_data = {
            "tmdbId": tmdb_id,
            "title": title,
            "qualityProfileId": profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "addOptions": {"searchForMovie": True},
        }
        return cast(dict[str, object], self._post("/api/v3/movie", movie_data))

    def get_movie_by_tmdb(self, tmdb_id: int) -> dict[str, object] | None:
        """Find a movie in the library by its TMDB ID.  Returns None if not found.

        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("get_movie_by_tmdb")
        for movie in self.get_movies():
            if movie.get("tmdbId") == tmdb_id:
                return movie
        logger.debug("get_movie_by_tmdb: no match for tmdb_id=%s", tmdb_id)
        return None
