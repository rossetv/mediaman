"""Radarr v3 API client."""

import requests


class RadarrClient:
    def __init__(self, url: str, api_key: str):
        self._url = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}

    def _get(self, path: str) -> dict | list:
        resp = requests.get(f"{self._url}{path}", headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, data: dict) -> None:
        resp = requests.put(f"{self._url}{path}", headers=self._headers, json=data, timeout=15)
        resp.raise_for_status()

    def _delete(self, path: str) -> None:
        resp = requests.delete(f"{self._url}{path}", headers=self._headers, timeout=15)
        resp.raise_for_status()

    def get_movies(self) -> list[dict]:
        return self._get("/api/v3/movie")

    def get_movie_by_id(self, movie_id: int) -> dict:
        return self._get(f"/api/v3/movie/{movie_id}")

    def delete_movie(self, movie_id: int) -> None:
        """Delete a movie from Radarr and its files from disk."""
        self._delete(f"/api/v3/movie/{movie_id}?deleteFiles=true&addImportExclusion=true")

    def unmonitor_movie(self, movie_id: int) -> None:
        movie = self.get_movie_by_id(movie_id)
        movie["monitored"] = False
        self._put(f"/api/v3/movie/{movie_id}", movie)

    def remonitor_movie(self, movie_id: int) -> None:
        movie = self.get_movie_by_id(movie_id)
        movie["monitored"] = True
        self._put(f"/api/v3/movie/{movie_id}", movie)
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

    def add_movie(self, tmdb_id: int, title: str, quality_profile_id: int = 4) -> dict:
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

    def get_queue(self) -> list[dict]:
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

    def get_movie_by_tmdb(self, tmdb_id: int) -> dict | None:
        """Find a movie in the library by its TMDB ID. Returns None if not found."""
        for movie in self.get_movies():
            if movie.get("tmdbId") == tmdb_id:
                return movie
        return None

    def lookup_movie(self, tmdb_id: int) -> dict | None:
        """Look up a movie by TMDB ID to check if it already exists."""
        try:
            results = self._get(f"/api/v3/movie/lookup/tmdb?tmdbId={tmdb_id}")
            return results if isinstance(results, dict) else None
        except Exception:
            return None

    def test_connection(self) -> bool:
        try:
            self._get("/api/v3/system/status")
            return True
        except Exception:
            return False
