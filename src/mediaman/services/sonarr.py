"""Sonarr v3 API client."""

from __future__ import annotations

import logging
from typing import cast

from mediaman.services.arr_client_base import ArrClient

logger = logging.getLogger("mediaman")


class SonarrClient(ArrClient):
    def delete_episode_files(self, series_id: int, season_number: int) -> None:
        """Delete all episode files for a season from disk via Sonarr."""
        efs = cast(list[dict[str, object]], self._get(f"/api/v3/episodefile?seriesId={series_id}"))
        for ef in efs:
            if ef.get("seasonNumber") == season_number:
                self._delete(f"/api/v3/episodefile/{ef['id']}")

    def delete_series(self, series_id: int) -> None:
        """Delete a series from Sonarr, its files, and add to exclusion list."""
        self._delete(f"/api/v3/series/{series_id}?deleteFiles=true&addImportListExclusion=true")

    def has_remaining_files(self, series_id: int) -> bool:
        """Return True if the series still has any episode files on disk."""
        efs = cast(list[dict[str, object]], self._get(f"/api/v3/episodefile?seriesId={series_id}"))
        return len(efs) > 0

    def get_series(self) -> list[dict[str, object]]:
        data = self._get("/api/v3/series")
        return cast(list[dict[str, object]], data) if isinstance(data, list) else []

    def get_series_by_id(self, series_id: int) -> dict[str, object]:
        data = self._get(f"/api/v3/series/{series_id}")
        if not isinstance(data, dict):
            raise ValueError(f"Sonarr returned unexpected response type for series {series_id}")
        return cast(dict[str, object], data)

    def get_episodes(self, series_id: int) -> list[dict[str, object]]:
        """Return all episodes for a given series."""
        data = self._get(f"/api/v3/episode?seriesId={series_id}")
        return cast(list[dict[str, object]], data) if isinstance(data, list) else []

    def get_episode_files(self, series_id: int) -> list[dict[str, object]]:
        """Return episode file records for a given series."""
        data = self._get(f"/api/v3/episodefile?seriesId={series_id}")
        return cast(list[dict[str, object]], data) if isinstance(data, list) else []

    def unmonitor_season(self, series_id: int, season_number: int) -> None:
        """Raises ValueError if the series payload contains no seasons list."""
        series = self.get_series_by_id(series_id)
        seasons = series.get("seasons")
        if not isinstance(seasons, list):
            raise ValueError(f"Sonarr series {series_id} has no 'seasons' list")
        for season in seasons:
            if season["seasonNumber"] == season_number:
                season["monitored"] = False
        self._put(f"/api/v3/series/{series_id}", cast(dict, series))

    def remonitor_season(self, series_id: int, season_number: int) -> None:
        """Raises ValueError if the series payload contains no seasons list."""
        series = self.get_series_by_id(series_id)
        seasons = series.get("seasons")
        if not isinstance(seasons, list):
            raise ValueError(f"Sonarr series {series_id} has no 'seasons' list")
        for season in seasons:
            if season["seasonNumber"] == season_number:
                season["monitored"] = True
        self._put(f"/api/v3/series/{series_id}", cast(dict, series))
        self._post("/api/v3/command", {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number})

    def search_series(self, series_id: int) -> None:
        """Trigger a Sonarr SeriesSearch command for a single series."""
        self._post("/api/v3/command", {"name": "SeriesSearch", "seriesId": series_id})

    def get_missing_series(self) -> dict[int, str]:
        """Return ``{series_id: series_title}`` for every series with at least
        one monitored missing episode, matching Sonarr's Wanted/Missing view.

        Series with zero episode files are included here too — callers that
        already handle the zero-file case should dedupe by ``series_id``.
        """
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

    def add_series(self, tvdb_id: int, title: str, quality_profile_id: int = 4) -> dict[str, object]:
        """Add a TV series by TVDB ID and trigger a search."""
        root_folders = cast(list[dict[str, object]], self._get("/api/v3/rootfolder"))
        root_path = root_folders[0]["path"] if root_folders else "/tv"

        series_data = {
            "tvdbId": tvdb_id,
            "title": title,
            "qualityProfileId": quality_profile_id,
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
        quality_profile_id: int = 4,
    ) -> dict[str, object]:
        """Add a series with an explicit per-season monitor/search plan.

        Seasons listed in ``monitored_seasons`` are added with
        ``monitored=True``; every other season (including season 0)
        is added with ``monitored=False``. The full-series auto-search
        is suppressed; instead a ``SeasonSearch`` command is issued
        for each season number in ``search_seasons``.
        """
        root_folders = cast(list[dict[str, object]], self._get("/api/v3/rootfolder"))
        root_path = root_folders[0]["path"] if root_folders else "/tv"

        lookup = cast(list[dict[str, object]], self._get(f"/api/v3/series/lookup?term=tvdb:{tvdb_id}"))
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
            "qualityProfileId": quality_profile_id,
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
            self._post("/api/v3/command", {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number})

        return new_series

    def get_queue(self) -> list[dict[str, object]]:
        """Return the current Sonarr download queue.

        Paginates through all pages; Sonarr defaults to a small pageSize
        and would otherwise silently truncate long queues — orphaning every
        NZB whose queue record sits past the first page.
        """
        out: list[dict] = []
        page = 1
        page_size = 500
        for _ in range(20):  # hard cap to prevent runaway paging
            data = self._get(
                f"/api/v3/queue?page={page}&pageSize={page_size}"
                "&includeSeries=true&includeEpisode=true"
            )
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

        Returns the first match or ``None`` if nothing matched or the
        call fails.
        """
        try:
            results = self._get(f"/api/v3/series/lookup?term=tmdb:{tmdb_id}")
            if isinstance(results, list) and results:
                return results[0]
            return None
        except Exception:
            logger.debug("lookup_series_by_tmdb failed for tmdb_id %s", tmdb_id, exc_info=True)
            return None
