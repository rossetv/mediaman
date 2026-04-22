"""Radarr v3 API client."""

from __future__ import annotations

import requests

from mediaman.services.arr_client_base import ArrClient


class RadarrClient(ArrClient):
    def get_movies(self) -> list[dict[str, object]]:
        return self._get("/api/v3/movie")  # type: ignore[return-value]

    def get_movie_by_id(self, movie_id: int) -> dict[str, object]:
        return self._get(f"/api/v3/movie/{movie_id}")  # type: ignore[return-value]

    def delete_movie(self, movie_id: int) -> None:
        """Delete a movie from Radarr and its files from disk."""
        self._delete(f"/api/v3/movie/{movie_id}?deleteFiles=true&addImportExclusion=true")

    def unmonitor_movie(self, movie_id: int) -> None:
        movie = self.get_movie_by_id(movie_id)
        movie["monitored"] = False
        self._put(f"/api/v3/movie/{movie_id}", movie)  # type: ignore[arg-type]

    def remonitor_movie(self, movie_id: int) -> None:
        movie = self.get_movie_by_id(movie_id)
        movie["monitored"] = True
        self._put(f"/api/v3/movie/{movie_id}", movie)  # type: ignore[arg-type]
        self.search_movie(movie_id)

    def search_movie(self, movie_id: int) -> None:
        """Trigger a Radarr MoviesSearch command for a single movie."""
        resp = requests.post(
            f"{self._url}/api/v3/command",
            headers=self._headers,
            json={"name": "MoviesSearch", "movieIds": [movie_id]},
            timeout=15,
        )
        resp.raise_for_status()

    def add_movie(self, tmdb_id: int, title: str, quality_profile_id: int = 4) -> dict[str, object]:
        """Add a movie by TMDB ID and trigger a search."""
        root_folders = self._get("/api/v3/rootfolder")
        root_path = root_folders[0]["path"] if root_folders else "/movies"

        movie_data = {
            "tmdbId": tmdb_id,
            "title": title,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "addOptions": {"searchForMovie": True},
        }
        resp = requests.post(
            f"{self._url}/api/v3/movie",
            headers=self._headers,
            json=movie_data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_queue(self) -> list[dict[str, object]]:
        """Return the current Radarr download queue.

        Paginates through all pages — otherwise long queues get silently
        truncated at the default page, orphaning NZBs past the cutoff.
        """
        out: list[dict] = []
        page = 1
        page_size = 500
        for _ in range(20):  # hard cap to prevent runaway paging
            data = self._get(
                f"/api/v3/queue?page={page}&pageSize={page_size}&includeMovie=true"
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

    def get_movie_by_tmdb(self, tmdb_id: int) -> dict[str, object] | None:
        """Find a movie in the library by its TMDB ID. Returns None if not found."""
        for movie in self.get_movies():
            if movie.get("tmdbId") == tmdb_id:
                return movie
        return None

