"""Radarr v3 API client."""

from __future__ import annotations

import logging
from typing import Any, cast

from mediaman.services.arr.base import ArrClient

logger = logging.getLogger("mediaman")


class RadarrClient(ArrClient):
    def get_movies(self) -> list[dict[str, Any]]:
        data = self._get("/api/v3/movie")
        return cast(list[dict[str, Any]], data) if isinstance(data, list) else []

    def get_movie_by_id(self, movie_id: int) -> dict[str, object]:
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
        """
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
        """
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
        movie = self.get_movie_by_id(movie_id)
        movie["monitored"] = True
        self._put(f"/api/v3/movie/{movie_id}", cast(dict, movie))
        self.search_movie(movie_id)

    def search_movie(self, movie_id: int) -> None:
        """Trigger a Radarr MoviesSearch command for a single movie."""
        self._post("/api/v3/command", {"name": "MoviesSearch", "movieIds": [movie_id]})

    def add_movie(
        self, tmdb_id: int, title: str, quality_profile_id: int | None = None
    ) -> dict[str, object]:
        """Add a movie by TMDB ID and trigger a search.

        ``quality_profile_id`` is selected via :meth:`_choose_quality_profile`
        when not specified.  Pass ``tmdb_id <= 0`` and the call raises
        :exc:`ValueError` rather than letting Radarr return an opaque 400.
        """
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

    def get_queue(self) -> list[dict[str, Any]]:
        """Return the current Radarr download queue.

        Paginates through all pages — otherwise long queues get silently
        truncated at the default page, orphaning NZBs past the cutoff.
        """
        out: list[dict[str, Any]] = []
        page = 1
        page_size = 500
        for _ in range(20):  # hard cap to prevent runaway paging
            data = self._get(f"/api/v3/queue?page={page}&pageSize={page_size}&includeMovie=true")
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

    def get_movie_by_tmdb(self, tmdb_id: int) -> dict[str, object] | None:
        """Find a movie in the library by its TMDB ID. Returns None if not found."""
        for movie in self.get_movies():
            if movie.get("tmdbId") == tmdb_id:
                return movie
        logger.debug("get_movie_by_tmdb: no match for tmdb_id=%s", tmdb_id)
        return None
