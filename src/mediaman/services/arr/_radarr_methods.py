"""Radarr-specific methods for :class:`~mediaman.services.arr.base.ArrClient`.

Mixed into the unified client.  Every method here calls
``self._require_movie(...)`` first so a Sonarr-flavoured client gets
:exc:`ArrKindMismatch` instead of a confusing 404 from the Radarr URL
shape.
"""

from __future__ import annotations

import logging
from typing import cast

from mediaman.services.arr._transport import ArrUpstreamError
from mediaman.services.arr._types import RadarrMovie

logger = logging.getLogger(__name__)


class _RadarrMixin:
    """Radarr (``kind="movie"``) operations.

    Every method asserts the client kind via
    :meth:`~mediaman.services.arr.base.ArrClient._require_movie` so a
    Sonarr-flavoured client raises :exc:`ArrKindMismatch` rather than
    silently issuing the wrong URL shape.
    """

    def get_movies(self) -> list[RadarrMovie]:
        """Return all movies in the Radarr library."""
        self._require_movie("get_movies")  # type: ignore[attr-defined]
        data = self._get("/api/v3/movie")  # type: ignore[attr-defined]
        return cast(list[RadarrMovie], data) if isinstance(data, list) else []

    def get_movie_by_id(self, movie_id: int) -> dict[str, object]:
        """Return a single movie by its Radarr ID.

        Raises :exc:`ArrUpstreamError` when Radarr returns a non-dict response.
        """
        self._require_movie("get_movie_by_id")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/movie/{movie_id}")  # type: ignore[attr-defined]
        if not isinstance(data, dict):
            raise ArrUpstreamError(
                f"Radarr returned unexpected type for movie {movie_id}: {type(data).__name__}"
            )
        return data

    def delete_movie(self, movie_id: int) -> None:
        """Delete a movie from Radarr and its files from disk."""
        self._require_movie("delete_movie")  # type: ignore[attr-defined]
        # ``self.spec.exclusion_param`` carries the per-flavour spelling
        # — see :meth:`delete_series` for the rationale.
        self._delete(  # type: ignore[attr-defined]
            f"/api/v3/movie/{movie_id}?deleteFiles=true&{self.spec.exclusion_param}=true"  # type: ignore[attr-defined]
        )

    def unmonitor_movie(self, movie_id: int, *, max_retries: int = 3) -> None:
        """Set ``monitored=False`` for *movie_id* in Radarr.

        Radarr v3 has no PATCH endpoint, so this is a read-modify-write
        on the full movie payload.  The retry loop tolerates concurrent
        writers — see :func:`_unmonitor_with_retry` for the details.
        """
        self._require_movie("unmonitor_movie")  # type: ignore[attr-defined]

        self._unmonitor_with_retry(  # type: ignore[attr-defined]
            fetch_entity=lambda: cast(dict, self.get_movie_by_id(movie_id)),
            put_url=f"/api/v3/movie/{movie_id}",
            is_already_unmonitored=lambda movie: not bool(movie.get("monitored", False)),
            apply_unmonitor=lambda movie: movie.__setitem__("monitored", False),
            log_prefix="radarr.unmonitor_movie",
            log_id=f"movie_id={movie_id}",
            max_retries=max_retries,
        )

    def remonitor_movie(self, movie_id: int) -> None:
        """Set ``monitored=True`` for *movie_id* and trigger a search."""
        self._require_movie("remonitor_movie")  # type: ignore[attr-defined]
        movie = self.get_movie_by_id(movie_id)
        movie["monitored"] = True
        self._put(f"/api/v3/movie/{movie_id}", cast(dict, movie))  # type: ignore[attr-defined]
        self.search_movie(movie_id)

    def search_movie(self, movie_id: int) -> None:
        """Trigger a Radarr MoviesSearch command for a single movie."""
        self._require_movie("search_movie")  # type: ignore[attr-defined]
        self._post(  # type: ignore[attr-defined]
            "/api/v3/command", {"name": "MoviesSearch", "movieIds": [movie_id]}
        )

    def add_movie(
        self, tmdb_id: int, title: str, quality_profile_id: int | None = None
    ) -> dict[str, object]:
        """Add a movie by TMDB ID and trigger a search.

        ``quality_profile_id`` is selected via
        :meth:`_choose_quality_profile` when not specified.  Raises
        :exc:`ValueError` if ``tmdb_id <= 0`` rather than letting Radarr
        return an opaque 400.
        """
        self._require_movie("add_movie")  # type: ignore[attr-defined]
        if tmdb_id <= 0:
            raise ValueError(f"tmdb_id must be positive, got {tmdb_id!r}")
        root_path = self._choose_root_folder()  # type: ignore[attr-defined]
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()  # type: ignore[attr-defined]
        )
        movie_data = {
            "tmdbId": tmdb_id,
            "title": title,
            "qualityProfileId": profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "addOptions": {"searchForMovie": True},
        }
        return cast(dict[str, object], self._post("/api/v3/movie", movie_data))  # type: ignore[attr-defined]

    def get_movie_by_tmdb(self, tmdb_id: int) -> RadarrMovie | None:
        """Find a movie in the library by its TMDB ID, or ``None`` if not found."""
        self._require_movie("get_movie_by_tmdb")  # type: ignore[attr-defined]
        for movie in self.get_movies():
            if movie.get("tmdbId") == tmdb_id:
                return movie
        logger.debug("get_movie_by_tmdb: no match for tmdb_id=%s", tmdb_id)
        return None
