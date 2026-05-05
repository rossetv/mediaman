"""Shared HTTP base and spec-driven unified client for *arr-family APIs.

This module provides two classes:

* :class:`_ArrClientBase` — raw HTTP helpers (GET/PUT/POST/DELETE), the
  connection test, add-flow pickers (root folder, quality profile), and
  lookup helpers.  It is not instantiated directly; it is the superclass of
  :class:`ArrClient`.  The implementation lives in
  :mod:`mediaman.services.arr._client_base` and is re-exported here for
  back-compat.

* :class:`ArrClient` — the spec-driven unified client.  It accepts an
  :class:`~mediaman.services.arr.spec.ArrSpec` as its first constructor
  argument, which determines whether it talks to Sonarr (``kind="series"``)
  or Radarr (``kind="movie"``).  All service-specific methods are present on
  this class; kind-specific ones raise :exc:`ArrKindMismatch` when called on
  the wrong variant.

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

from mediaman.services.arr._client_base import (
    _ARR_TIMEOUT,
    ArrKindMismatch,
    _ArrClientBase,
)
from mediaman.services.arr.spec import ArrSpec
from mediaman.services.infra.http import SafeHTTPError

logger = logging.getLogger("mediaman")

# Re-export for back-compat: tests and other callers that import these names
# directly from ``mediaman.services.arr.base`` continue to work unchanged.
__all__ = [
    "_ARR_TIMEOUT",
    "ArrClient",
    "ArrKindMismatch",
    "_ArrClientBase",
]


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

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("delete_series")
        # ``self.spec.exclusion_param`` is the per-flavour spelling
        # (``addImportListExclusion`` for Sonarr, ``addImportExclusion`` for
        # Radarr).  Reading it through the spec keeps the spelling in one
        # place — see :class:`mediaman.services.arr.spec.ArrSpec`.
        self._delete(
            f"/api/v3/series/{series_id}?deleteFiles=true&{self.spec.exclusion_param}=true"
        )

    def has_remaining_files(self, series_id: int) -> bool:
        """Return True if the series still has any episode files on disk.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("has_remaining_files")
        efs = cast(list[dict[str, object]], self._get(f"/api/v3/episodefile?seriesId={series_id}"))
        return bool(efs)

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

        def fetch_entity() -> dict:
            series = self.get_series_by_id(series_id)
            seasons = series.get("seasons")
            if not isinstance(seasons, list):
                raise ValueError(f"Sonarr series {series_id} has no 'seasons' list")
            if not any(s.get("seasonNumber") == season_number for s in seasons):
                raise ValueError(f"Sonarr series {series_id} has no season {season_number}")
            return cast(dict, series)

        def is_already_unmonitored(entity: dict) -> bool:
            seasons = entity.get("seasons", [])
            target = next((s for s in seasons if s.get("seasonNumber") == season_number), None)
            if target is None:
                raise ValueError(f"Sonarr series {series_id} has no season {season_number}")
            return not bool(target.get("monitored", False))

        def apply_unmonitor(entity: dict) -> None:
            seasons = entity.get("seasons", [])
            target = next((s for s in seasons if s.get("seasonNumber") == season_number), None)
            if target is not None:
                target["monitored"] = False

        self._unmonitor_with_retry(
            fetch_entity=fetch_entity,
            put_url=f"/api/v3/series/{series_id}",
            is_already_unmonitored=is_already_unmonitored,
            apply_unmonitor=apply_unmonitor,
            log_prefix="sonarr.unmonitor_season",
            log_id=f"series_id={series_id} season={season_number}",
            max_retries=max_retries,
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

        Raises :exc:`ArrKindMismatch` when called on a Sonarr client.
        """
        self._require_movie("delete_movie")
        # ``self.spec.exclusion_param`` carries the per-flavour spelling —
        # see :func:`delete_series` for the rationale.
        self._delete(f"/api/v3/movie/{movie_id}?deleteFiles=true&{self.spec.exclusion_param}=true")

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

        self._unmonitor_with_retry(
            fetch_entity=lambda: cast(dict, self.get_movie_by_id(movie_id)),
            put_url=f"/api/v3/movie/{movie_id}",
            is_already_unmonitored=lambda movie: not bool(movie.get("monitored", False)),
            apply_unmonitor=lambda movie: movie.__setitem__("monitored", False),
            log_prefix="radarr.unmonitor_movie",
            log_id=f"movie_id={movie_id}",
            max_retries=max_retries,
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
