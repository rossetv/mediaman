"""Sonarr-specific methods for :class:`~mediaman.services.arr.base.ArrClient`.

Mixed into the unified client.  Every method here calls
``self._require_series(...)`` first so a Radarr-flavoured client gets
:exc:`ArrKindMismatch` instead of a confusing 404 from the Sonarr URL
shape.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from mediaman.services.arr._transport import ArrConfigError
from mediaman.services.infra.http import SafeHTTPError

logger = logging.getLogger(__name__)


class _SonarrMixin:
    """Sonarr (``kind="series"``) operations.

    Every method asserts the client kind via
    :meth:`~mediaman.services.arr.base.ArrClient._require_series` so a
    Radarr-flavoured client raises :exc:`ArrKindMismatch` rather than
    silently issuing the wrong URL shape.
    """

    def delete_episode_files(self, series_id: int, season_number: int) -> None:
        """Delete all episode files for a season from disk via Sonarr.

        Uses the ``/api/v3/episodefile/bulk`` endpoint (single POST with
        an ``episodeFileIds`` list) rather than N serial DELETEs, which
        would hammer Sonarr on large seasons.

        Raises :exc:`ArrKindMismatch` when called on a Radarr client.
        """
        self._require_series("delete_episode_files")  # type: ignore[attr-defined]
        efs = cast(
            list[dict[str, Any]],
            self._get(f"/api/v3/episodefile?seriesId={series_id}"),  # type: ignore[attr-defined]
        )
        ids = [
            int(ef["id"])
            for ef in efs
            if ef.get("seasonNumber") == season_number and ef.get("id") is not None
        ]
        if ids:
            self._delete_bulk_episode_files(ids)

    def _delete_bulk_episode_files(self, episode_file_ids: list[int]) -> None:
        """Delete multiple episode files in a single Sonarr API call.

        Calls ``DELETE /api/v3/episodefile/bulk`` with
        ``{"episodeFileIds": [...]}`` — supported since Sonarr v3.
        Falls back to serial deletes when the endpoint returns 404
        (Sonarr version too old).
        """
        try:
            self._http.delete(  # type: ignore[attr-defined]
                "/api/v3/episodefile/bulk",
                headers=self._headers,  # type: ignore[attr-defined]
                json={"episodeFileIds": episode_file_ids},
            )
        except SafeHTTPError as exc:
            if exc.status_code == 404:
                logger.debug(
                    "sonarr.delete_episode_files: bulk endpoint not available "
                    "(HTTP 404), falling back to serial deletes"
                )
                for ef_id in episode_file_ids:
                    self._delete(f"/api/v3/episodefile/{ef_id}")  # type: ignore[attr-defined]
            else:
                raise

    def delete_series(self, series_id: int) -> None:
        """Delete a series from Sonarr, its files, and add to exclusion list."""
        self._require_series("delete_series")  # type: ignore[attr-defined]
        # ``self.spec.exclusion_param`` carries the per-flavour spelling
        # (``addImportListExclusion`` for Sonarr, ``addImportExclusion``
        # for Radarr).  Reading through the spec keeps the spelling in
        # one place — see :class:`mediaman.services.arr.spec.ArrSpec`.
        self._delete(  # type: ignore[attr-defined]
            f"/api/v3/series/{series_id}?deleteFiles=true&{self.spec.exclusion_param}=true"  # type: ignore[attr-defined]
        )

    def has_remaining_files(self, series_id: int) -> bool:
        """Return True if the series still has any episode files on disk."""
        self._require_series("has_remaining_files")  # type: ignore[attr-defined]
        efs = cast(
            list[dict[str, object]],
            self._get(f"/api/v3/episodefile?seriesId={series_id}"),  # type: ignore[attr-defined]
        )
        return bool(efs)

    def get_series(self) -> list[dict[str, Any]]:
        """Return all series in the Sonarr library."""
        self._require_series("get_series")  # type: ignore[attr-defined]
        data = self._get("/api/v3/series")  # type: ignore[attr-defined]
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def get_series_by_id(self, series_id: int) -> dict[str, Any]:
        """Return a single series by its Sonarr ID.

        Raises :exc:`ValueError` when Sonarr returns a non-dict response.
        """
        self._require_series("get_series_by_id")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/series/{series_id}")  # type: ignore[attr-defined]
        if not isinstance(data, dict):
            raise ValueError(f"Sonarr returned unexpected response type for series {series_id}")
        return cast(dict[str, Any], data)

    def get_episodes(self, series_id: int) -> list[dict[str, Any]]:
        """Return all episodes for a given series."""
        self._require_series("get_episodes")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/episode?seriesId={series_id}")  # type: ignore[attr-defined]
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def get_episode_files(self, series_id: int) -> list[dict[str, Any]]:
        """Return episode file records for a given series."""
        self._require_series("get_episode_files")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/episodefile?seriesId={series_id}")  # type: ignore[attr-defined]
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def unmonitor_season(self, series_id: int, season_number: int, *, max_retries: int = 3) -> None:
        """Set ``monitored=False`` for *season_number* of *series_id*.

        Sonarr v3 has no PATCH endpoint, so this is a read-modify-write
        on the full series payload.  The retry loop tolerates concurrent
        writers — see :func:`_unmonitor_with_retry` for the details.
        """
        self._require_series("unmonitor_season")  # type: ignore[attr-defined]

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

        self._unmonitor_with_retry(  # type: ignore[attr-defined]
            fetch_entity=fetch_entity,
            put_url=f"/api/v3/series/{series_id}",
            is_already_unmonitored=is_already_unmonitored,
            apply_unmonitor=apply_unmonitor,
            log_prefix="sonarr.unmonitor_season",
            log_id=f"series_id={series_id} season={season_number}",
            max_retries=max_retries,
        )

    def remonitor_season(self, series_id: int, season_number: int) -> None:
        """Set ``monitored=True`` for *season_number* and trigger a search."""
        self._require_series("remonitor_season")  # type: ignore[attr-defined]
        series = self.get_series_by_id(series_id)
        seasons = series.get("seasons")
        if not isinstance(seasons, list):
            raise ValueError(f"Sonarr series {series_id} has no 'seasons' list")
        for season in seasons:
            if season["seasonNumber"] == season_number:
                season["monitored"] = True
        self._put(f"/api/v3/series/{series_id}", cast(dict, series))  # type: ignore[attr-defined]
        self._post(  # type: ignore[attr-defined]
            "/api/v3/command",
            {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number},
        )

    def search_series(self, series_id: int) -> None:
        """Trigger a Sonarr SeriesSearch command for a single series."""
        self._require_series("search_series")  # type: ignore[attr-defined]
        self._post("/api/v3/command", {"name": "SeriesSearch", "seriesId": series_id})  # type: ignore[attr-defined]

    def get_missing_series(self) -> dict[int, str]:
        """Return ``{series_id: series_title}`` for every series with at least
        one monitored missing episode, matching Sonarr's Wanted/Missing view.
        """
        self._require_series("get_missing_series")  # type: ignore[attr-defined]
        out: dict[int, str] = {}
        page = 1
        page_size = 250
        for _ in range(100):  # hard cap to prevent runaway paging
            data = self._get(  # type: ignore[attr-defined]
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

        ``quality_profile_id`` is selected via
        :meth:`_choose_quality_profile` when not specified.  Raises
        :exc:`ValueError` if ``tvdb_id <= 0`` rather than letting Sonarr
        return an opaque 400.
        """
        self._require_series("add_series")  # type: ignore[attr-defined]
        if tvdb_id <= 0:
            raise ValueError(f"tvdb_id must be positive, got {tvdb_id!r}")
        root_path = self._choose_root_folder()  # type: ignore[attr-defined]
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()  # type: ignore[attr-defined]
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
        return cast(dict[str, object], self._post("/api/v3/series", series_data))  # type: ignore[attr-defined]

    def add_series_with_seasons(
        self,
        tvdb_id: int,
        title: str,
        monitored_seasons: list[int],
        search_seasons: list[int],
        quality_profile_id: int | None = None,
    ) -> dict[str, object]:
        """Add a series with an explicit per-season monitor/search plan.

        Seasons listed in *monitored_seasons* are added with
        ``monitored=True``; every other season (including season 0) is
        added with ``monitored=False``.  The full-series auto-search is
        suppressed; instead a ``SeasonSearch`` command is issued for
        each season number in *search_seasons*.
        """
        self._require_series("add_series_with_seasons")  # type: ignore[attr-defined]
        if tvdb_id <= 0:
            raise ValueError(f"tvdb_id must be positive, got {tvdb_id!r}")
        body = self._prepare_series_with_seasons_body(
            tvdb_id, title, monitored_seasons, quality_profile_id
        )
        new_series = cast(dict[str, object], self._post("/api/v3/series", body))  # type: ignore[attr-defined]
        series_id = new_series.get("id")
        for season_number in search_seasons:
            self._post(  # type: ignore[attr-defined]
                "/api/v3/command",
                {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number},
            )
        return new_series

    def _prepare_series_with_seasons_body(
        self,
        tvdb_id: int,
        title: str,
        monitored_seasons: list[int],
        quality_profile_id: int | None,
    ) -> dict[str, object]:
        """Build the POST body for :meth:`add_series_with_seasons`."""
        root_path = self._choose_root_folder()  # type: ignore[attr-defined]
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()  # type: ignore[attr-defined]
        )

        lookup = cast(
            list[dict[str, Any]],
            self._get(f"/api/v3/series/lookup?term=tvdb:{tvdb_id}"),  # type: ignore[attr-defined]
        )
        if not lookup:
            raise ArrConfigError(f"Sonarr lookup returned no results for tvdb:{tvdb_id}")
        meta = lookup[0]

        monitored_set = set(monitored_seasons)
        seasons_payload = [
            {
                "seasonNumber": s["seasonNumber"],
                "monitored": s["seasonNumber"] in monitored_set,
            }
            for s in meta.get("seasons", [])
        ]
        return {
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

    def lookup_series_by_tmdb(self, tmdb_id: int) -> dict[str, object] | None:
        """Look up a series by TMDB ID via Sonarr's lookup endpoint.

        Returns the first match or ``None`` if Sonarr's metadata provider
        does not know the series.  Network failures propagate so callers
        can distinguish "not found" from "call failed".
        """
        self._require_series("lookup_series_by_tmdb")  # type: ignore[attr-defined]
        results = self.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")  # type: ignore[attr-defined]
        return results[0] if results else None
