"""Sonarr-specific methods for ``ArrClient``.

Mixed into the unified client.  Every method here calls
``self._require_series(...)`` first so a Radarr-flavoured client gets
``ArrKindMismatch`` instead of a confusing 404 from the Sonarr URL
shape.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import cast

from mediaman.services.arr._transport import ArrConfigError, ArrUpstreamError
from mediaman.services.arr._types import ArrEpisode, ArrEpisodeFile, ArrLookupResult, SonarrSeries
from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)


class _SonarrMixin:
    """Sonarr (``kind="series"``) operations.

    Every method asserts the client kind via
    ``_require_series`` so a
    Radarr-flavoured client raises ``ArrKindMismatch`` rather than
    silently issuing the wrong URL shape.
    """

    def delete_episode_files(self, episode_file_ids: Sequence[int]) -> None:
        """Delete the given episode files from disk via Sonarr.

        Takes ids rather than a season so only files the caller has checked
        are deleted, never one imported since.  Calls
        ``DELETE /api/v3/episodefile/bulk`` with ``{"episodeFileIds": [...]}``
        (one request, supported since Sonarr v3) and falls back to serial
        deletes when that endpoint answers 404 (Sonarr too old).  In the
        fallback a 404 for one file means Sonarr already dropped it, so the
        rest are still deleted.

        Raises ``ArrKindMismatch`` when called on a Radarr client.
        """
        self._require_series("delete_episode_files")  # type: ignore[attr-defined]
        ids = list(episode_file_ids)
        if not ids:
            return
        try:
            self._http.delete(  # type: ignore[attr-defined]
                "/api/v3/episodefile/bulk",
                headers=self._headers,  # type: ignore[attr-defined]
                json={"episodeFileIds": ids},
            )
        except SafeHTTPError as exc:
            if exc.status_code != 404:
                raise
            logger.debug(
                "sonarr.delete_episode_files: bulk endpoint not available "
                "(HTTP 404), falling back to serial deletes"
            )
            for ef_id in ids:
                try:
                    self._delete(f"/api/v3/episodefile/{ef_id}")  # type: ignore[attr-defined]
                except SafeHTTPError as file_exc:
                    if file_exc.status_code != 404:
                        raise

    def delete_series(self, series_id: int) -> None:
        """Delete a series from Sonarr, its files, and add to exclusion list."""
        self._require_series("delete_series")  # type: ignore[attr-defined]
        # ``self.spec.exclusion_param`` carries the per-flavour spelling
        # (``addImportListExclusion`` for Sonarr, ``addImportExclusion``
        # for Radarr).  Reading through the spec keeps the spelling in
        # one place — see ``mediaman.services.arr.spec.ArrSpec``.
        self._delete(  # type: ignore[attr-defined]
            f"/api/v3/series/{series_id}?deleteFiles=true&{self.spec.exclusion_param}=true"  # type: ignore[attr-defined]
        )

    def get_series(self) -> list[SonarrSeries]:
        self._require_series("get_series")  # type: ignore[attr-defined]
        data = self._get("/api/v3/series")  # type: ignore[attr-defined]
        return cast(list[SonarrSeries], data) if isinstance(data, list) else []

    def get_series_by_id(self, series_id: int) -> SonarrSeries:
        """Return a single series by its Sonarr ID.

        Raises ``ArrUpstreamError`` when Sonarr returns a non-dict response.
        """
        self._require_series("get_series_by_id")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/series/{series_id}")  # type: ignore[attr-defined]
        if not isinstance(data, dict):
            raise ArrUpstreamError(
                f"Sonarr returned unexpected response type for series {series_id}"
            )
        return cast(SonarrSeries, data)

    def get_series_by_tvdb(self, tvdb_id: int) -> SonarrSeries | None:
        """Find a series in the library by its TVDB ID, or ``None`` if not found.

        Uses Sonarr's ``GET /api/v3/series?tvdbId=`` server-side filter,
        which returns a (possibly empty) list of matching library entries.
        """
        self._require_series("get_series_by_tvdb")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/series?tvdbId={tvdb_id}")  # type: ignore[attr-defined]
        if not isinstance(data, list) or not data:
            logger.debug("get_series_by_tvdb: no match for tvdb_id=%s", tvdb_id)
            return None
        first = data[0]
        return cast(SonarrSeries, first) if isinstance(first, dict) else None

    def get_episodes(self, series_id: int) -> list[ArrEpisode]:
        self._require_series("get_episodes")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/episode?seriesId={series_id}")  # type: ignore[attr-defined]
        return cast(list[ArrEpisode], data) if isinstance(data, list) else []

    def get_episode_files(self, series_id: int) -> list[ArrEpisodeFile]:
        self._require_series("get_episode_files")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/episodefile?seriesId={series_id}")  # type: ignore[attr-defined]
        return cast(list[ArrEpisodeFile], data) if isinstance(data, list) else []

    def unmonitor_season(self, series_id: int, season_number: int, *, max_retries: int = 3) -> None:
        """Set ``monitored=False`` for *season_number* of *series_id*.

        Sonarr v3 has no PATCH endpoint, so this is a read-modify-write
        on the full series payload.  The retry loop tolerates concurrent
        writers — see ``_unmonitor_with_retry`` for the details.
        """
        self._require_series("unmonitor_season")  # type: ignore[attr-defined]

        def fetch_entity() -> SonarrSeries:
            series = self.get_series_by_id(series_id)
            seasons = series.get("seasons")
            if not isinstance(seasons, list):
                raise ArrUpstreamError(f"Sonarr series {series_id} has no 'seasons' list")
            if not any(s.get("seasonNumber") == season_number for s in seasons):
                raise ArrUpstreamError(f"Sonarr series {series_id} has no season {season_number}")
            return series

        def is_already_unmonitored(entity: SonarrSeries) -> bool:
            seasons = entity.get("seasons", [])
            target = next((s for s in seasons if s.get("seasonNumber") == season_number), None)
            if target is None:
                raise ArrUpstreamError(f"Sonarr series {series_id} has no season {season_number}")
            return not bool(target.get("monitored", False))

        def apply_unmonitor(entity: SonarrSeries) -> None:
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

    def remonitor_season(self, series_id: int, season_number: int, *, max_retries: int = 3) -> None:
        """Set ``monitored=True`` for *season_number* and trigger a search.

        Uses the same retry helper as ``unmonitor_season`` so a transient
        PUT failure doesn't silently strand the season in an unmonitored state.

        Raises ``ArrUpstreamError`` when the target season number is absent
        from the series payload — this prevents a silent no-op where the PUT
        succeeds but the season was never remonitored, which would be followed
        by a SeasonSearch for a season Sonarr never re-monitored.
        """
        self._require_series("remonitor_season")  # type: ignore[attr-defined]

        def fetch_entity() -> SonarrSeries:
            series = self.get_series_by_id(series_id)
            seasons = series.get("seasons")
            if not isinstance(seasons, list):
                raise ArrUpstreamError(f"Sonarr series {series_id} has no 'seasons' list")
            if not any(s.get("seasonNumber") == season_number for s in seasons):
                raise ArrUpstreamError(f"Sonarr series {series_id} has no season {season_number}")
            return series

        def is_already_remonitored(entity: SonarrSeries) -> bool:
            seasons = entity.get("seasons", [])
            target = next((s for s in seasons if s.get("seasonNumber") == season_number), None)
            if target is None:
                raise ArrUpstreamError(f"Sonarr series {series_id} has no season {season_number}")
            # "already done" for remonitor means it's already monitored.
            return bool(target.get("monitored", False))

        def apply_remonitor(entity: SonarrSeries) -> None:
            seasons = entity.get("seasons", [])
            target = next((s for s in seasons if s.get("seasonNumber") == season_number), None)
            if target is not None:
                target["monitored"] = True

        self._unmonitor_with_retry(  # type: ignore[attr-defined]
            fetch_entity=fetch_entity,
            put_url=f"/api/v3/series/{series_id}",
            is_already_unmonitored=is_already_remonitored,
            apply_unmonitor=apply_remonitor,
            log_prefix="sonarr.remonitor_season",
            log_id=f"series_id={series_id} season={season_number}",
            max_retries=max_retries,
        )
        self._post(  # type: ignore[attr-defined]
            "/api/v3/command",
            {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number},
        )

    def search_series(self, series_id: int) -> None:
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
        max_pages = 100
        for _ in range(max_pages):  # hard cap to prevent runaway paging
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
        else:
            # Exited via the page-count cap, not the totalRecords condition —
            # the wanted/missing list is being silently truncated. Warn so the
            # un-poked series past the cap are observable.
            logger.warning(
                "sonarr.get_missing_series: hit the %d-page cap (%d series) — "
                "wanted/missing list may be truncated",
                max_pages,
                len(out),
            )
        return out

    def add_series(
        self, tvdb_id: int, title: str, quality_profile_id: int | None = None
    ) -> SonarrSeries:
        """Add a TV series by TVDB ID and trigger a search.

        ``quality_profile_id`` is selected via
        ``_choose_quality_profile`` when not specified.  Raises
        ``ValueError`` if ``tvdb_id <= 0`` rather than letting Sonarr
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
        return cast(SonarrSeries, self._post("/api/v3/series", series_data))  # type: ignore[attr-defined]

    def add_series_with_seasons(
        self,
        tvdb_id: int,
        title: str,
        monitored_seasons: list[int],
        search_seasons: list[int],
        quality_profile_id: int | None = None,
    ) -> SonarrSeries:
        """Add a series with an explicit per-season monitor/search plan.

        Seasons listed in *monitored_seasons* are added with
        ``monitored=True``; every other season (including season 0) is
        added with ``monitored=False``.  ``monitor: skip`` keeps exactly those
        seasons (``none`` would unmonitor them all after the first scan) and
        Sonarr itself searches the monitored missing episodes once it has
        scanned the series, so no search command is posted here: one sent
        straight after the add finds no episodes yet.  *search_seasons* is
        accepted for the callers' sake; what Sonarr searches follows
        *monitored_seasons* alone, and a monitored season whose files are on
        disk has nothing missing to search.
        """
        self._require_series("add_series_with_seasons")  # type: ignore[attr-defined]
        if tvdb_id <= 0:
            raise ValueError(f"tvdb_id must be positive, got {tvdb_id!r}")
        body = self._prepare_series_with_seasons_body(
            tvdb_id, title, monitored_seasons, quality_profile_id
        )
        return cast(SonarrSeries, self._post("/api/v3/series", body))  # type: ignore[attr-defined]

    def _prepare_series_with_seasons_body(
        self,
        tvdb_id: int,
        title: str,
        monitored_seasons: list[int],
        quality_profile_id: int | None,
    ) -> dict[str, object]:
        root_path = self._choose_root_folder()  # type: ignore[attr-defined]
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()  # type: ignore[attr-defined]
        )

        lookup = cast(
            list[ArrLookupResult],
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
            # Sonarr's default monitors every season announced later; only the ones picked are wanted.
            "monitorNewItems": "none",
            "seasonFolder": True,
            "seasons": seasons_payload,
            "addOptions": {
                "searchForMissingEpisodes": True,
                "monitor": "skip",
            },
        }

    def lookup_series_by_tmdb(self, tmdb_id: int) -> ArrLookupResult | None:
        """Look up a series by TMDB ID via Sonarr's lookup endpoint.

        Returns the first match or ``None`` if Sonarr's metadata provider
        does not know the series.  Network failures propagate so callers
        can distinguish "not found" from "call failed".
        """
        self._require_series("lookup_series_by_tmdb")  # type: ignore[attr-defined]
        results = self.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")  # type: ignore[attr-defined]
        return results[0] if results else None
