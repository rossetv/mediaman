"""Arr-date cache — cross-container download-date lookup for media files.

Radarr and Sonarr track the exact date a file landed on disk, which is
more accurate than Plex's ``addedAt`` (Plex records the scan time, not
the download time). This module keeps a lazily-built lookup of
normalised-path to ISO download-date strings, queried during scan-time
eligibility checks.

Extracted from :mod:`mediaman.scanner.engine` so that constructing the
scan engine doesn't trigger Radarr/Sonarr I/O and so the cache logic
has a single clear home.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mediaman")


def normalise_path(path: str) -> str:
    """Strip container-specific root prefixes for cross-container matching.

    Plex, Radarr, and Sonarr each mount the same directories under
    different roots (e.g. ``/data/movies/...``, ``/movies/...``).
    This strips the first path component so matching works regardless
    of container mount point.
    """
    # "/data/movies/Film (2020)/Film.mkv" -> "movies/Film (2020)/Film.mkv"
    # "/movies/Film (2020)/Film.mkv"      -> "movies/Film (2020)/Film.mkv"
    parts = path.strip("/").split("/", 1)
    if len(parts) < 2:
        return path
    # If first component is a generic root like "data", strip it too.
    if parts[0] in ("data", "media", "share"):
        return parts[1]
    return path.strip("/")


class ArrDateCache:
    """Lazily-built lookup of normalised file paths to Arr download dates.

    The cache is built at most once per instance. Constructing the
    cache does not fire any network calls; the first call to
    :meth:`get` (or explicit :meth:`ensure_loaded`) triggers the
    Radarr/Sonarr reads.
    """

    def __init__(self, *, radarr_client: Any = None, sonarr_client: Any = None) -> None:
        self._radarr = radarr_client
        self._sonarr = sonarr_client
        self._dates: dict[str, str] = {}
        self._loaded = False

    def ensure_loaded(self) -> None:
        """Build the cache if it hasn't been built yet."""
        if not self._loaded:
            self._build()
            self._loaded = True

    def get(self, file_path: str) -> str | None:
        """Return the Arr download date for *file_path* (or ``None``)."""
        self.ensure_loaded()
        return self._dates.get(normalise_path(file_path))

    def _build(self) -> None:
        """Populate the lookup from Radarr and Sonarr."""
        # Radarr: movieFile.dateAdded keyed by movie file path.
        if self._radarr:
            try:
                for movie in self._radarr.get_movies():
                    mf = movie.get("movieFile")
                    if mf and mf.get("path") and mf.get("dateAdded"):
                        key = normalise_path(mf["path"])
                        self._dates[key] = mf["dateAdded"]
            except Exception:
                logger.warning(
                    "Failed to fetch Radarr dates — falling back to Plex",
                    exc_info=True,
                )

        # Sonarr: episodefile.dateAdded keyed by season directory -> latest date.
        if self._sonarr:
            try:
                for series in self._sonarr.get_series():
                    try:
                        efs = self._sonarr.get_episode_files(series["id"])
                        for ef in efs:
                            path = ef.get("path", "")
                            date_added = ef.get("dateAdded", "")
                            if path and date_added:
                                season_dir = path.rsplit("/", 1)[0]
                                key = normalise_path(season_dir)
                                existing = self._dates.get(key, "")
                                if date_added > existing:
                                    self._dates[key] = date_added
                    except Exception:
                        logger.warning(
                            "Failed to fetch episode files for series %s",
                            series.get("id"),
                            exc_info=True,
                        )
            except Exception:
                logger.warning(
                    "Failed to fetch Sonarr dates — falling back to Plex",
                    exc_info=True,
                )

        if self._dates:
            logger.info(
                "Cached %d download dates from Radarr/Sonarr", len(self._dates)
            )
