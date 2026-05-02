"""Sonarr v3 API client."""

from __future__ import annotations

import logging
from typing import Any, cast

from mediaman.services.arr.base import ArrClient

logger = logging.getLogger("mediaman")


class SonarrClient(ArrClient):
    def delete_episode_files(self, series_id: int, season_number: int) -> None:
        """Delete all episode files for a season from disk via Sonarr.

        Uses the ``/api/v3/episodefile/bulk`` endpoint (single POST with an
        ``episodeFileIds`` list) rather than N serial DELETEs, which would
        hammer Sonarr on large seasons.
        """
        efs = cast(list[dict[str, object]], self._get(f"/api/v3/episodefile?seriesId={series_id}"))
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
        from mediaman.services.infra.http_client import SafeHTTPError

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
        """
        sid = int(series_id)
        self._delete(f"/api/v3/series/{sid}?deleteFiles=true&addImportListExclusion=true")

    def has_remaining_files(self, series_id: int) -> bool:
        """Return True if the series still has any episode files on disk."""
        efs = cast(list[dict[str, object]], self._get(f"/api/v3/episodefile?seriesId={series_id}"))
        return len(efs) > 0

    def get_series(self) -> list[dict[str, Any]]:
        data = self._get("/api/v3/series")
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def get_series_by_id(self, series_id: int) -> dict[str, Any]:
        data = self._get(f"/api/v3/series/{series_id}")
        if not isinstance(data, dict):
            raise ValueError(f"Sonarr returned unexpected response type for series {series_id}")
        return cast(dict[str, Any], data)

    def get_episodes(self, series_id: int) -> list[dict[str, Any]]:
        """Return all episodes for a given series."""
        data = self._get(f"/api/v3/episode?seriesId={series_id}")
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def get_episode_files(self, series_id: int) -> list[dict[str, Any]]:
        """Return episode file records for a given series."""
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
        """
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
        """
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

    def add_series(
        self, tvdb_id: int, title: str, quality_profile_id: int | None = None
    ) -> dict[str, object]:
        """Add a TV series by TVDB ID and trigger a search.

        ``quality_profile_id`` is selected via :meth:`_choose_quality_profile`
        when not specified.  Pass ``tvdb_id <= 0`` and the call raises
        :exc:`ValueError` rather than letting Sonarr return an opaque 400.
        """
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
        """
        if tvdb_id <= 0:
            raise ValueError(f"tvdb_id must be positive, got {tvdb_id!r}")
        root_path = self._choose_root_folder()
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()
        )

        lookup = cast(
            list[dict[str, object]], self._get(f"/api/v3/series/lookup?term=tvdb:{tvdb_id}")
        )
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
        """Return the current Sonarr download queue.

        Paginates through all pages; Sonarr defaults to a small pageSize
        and would otherwise silently truncate long queues — orphaning every
        NZB whose queue record sits past the first page.
        """
        out: list[dict[str, Any]] = []
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

        Returns the first match or ``None`` if the upstream API returns an
        empty list (series genuinely not found in Sonarr's metadata provider).

        Network failures (:exc:`~mediaman.services.infra.http_client.SafeHTTPError`,
        :exc:`~requests.RequestException`) are allowed to propagate so callers
        can distinguish "not found" from "call failed".
        """
        results = self.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")
        return results[0] if results else None
