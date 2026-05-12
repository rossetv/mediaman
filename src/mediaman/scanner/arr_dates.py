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
from datetime import datetime
from typing import TYPE_CHECKING

import requests

from mediaman.services.arr.base import ArrError
from mediaman.services.infra import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)


def _parse_arr_iso(value: str) -> datetime | None:
    """Best-effort ISO-8601 parse of a Radarr/Sonarr date string.

    Both APIs sometimes return ``...Z`` (UTC indicator) and sometimes
    ``+00:00`` (offset form). ``datetime.fromisoformat`` accepts the
    offset form natively in Python 3.11+; the trailing ``Z`` is rewritten
    to ``+00:00`` to keep older interpreters happy too. Returns ``None``
    if the value is unparseable so the caller can fall back to a "keep
    whatever's already cached" path rather than substituting the wrong
    most-recent date.

    Kept bespoke rather than delegating to
    :func:`mediaman.services.infra.time.parse_iso_utc` because Arr APIs
    sometimes emit naive timestamps that the caller wants to keep naive
    for date-comparison logic — the canonical parser always attaches UTC.
    """
    if not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


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

    def __init__(
        self, *, radarr_client: ArrClient | None = None, sonarr_client: ArrClient | None = None
    ) -> None:
        self._radarr = radarr_client
        self._sonarr = sonarr_client
        self._dates: dict[str, str] = {}
        self._loaded = False

    def ensure_loaded(self) -> None:
        """Build the cache if it hasn't been built yet."""
        if not self._loaded:
            self._build()
            self._loaded = True

    def reset(self) -> None:
        """Force the cache to be rebuilt on the next :meth:`ensure_loaded` call."""
        self._loaded = False

    def dates(self) -> dict[str, str]:
        """Return the full path-to-date mapping (built lazily).

        Prefer :meth:`get` for individual lookups; this accessor exists for
        callers that need the raw dict (e.g. back-compat shims, testing).
        """
        self.ensure_loaded()
        return self._dates

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
            except (SafeHTTPError, requests.RequestException, ArrError):
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
                                existing = self._dates.get(key)
                                # Compare as datetimes, not strings — the
                                # two ISO forms ("...Z" and "...+00:00")
                                # are equivalent in time but compare
                                # incorrectly as plain strings, which
                                # silently dropped the more-recent date
                                # whenever a season had a mix of forms.
                                new_dt = _parse_arr_iso(date_added)
                                if new_dt is None:
                                    # Skip unparseable dates — leave any
                                    # cached value for the season alone.
                                    continue
                                existing_dt = _parse_arr_iso(existing) if existing else None
                                if existing_dt is None or new_dt > existing_dt:
                                    self._dates[key] = date_added
                    except (SafeHTTPError, requests.RequestException, ArrError):
                        logger.warning(
                            "Failed to fetch episode files for series %s",
                            series.get("id"),
                            exc_info=True,
                        )
            except (SafeHTTPError, requests.RequestException, ArrError):
                logger.warning(
                    "Failed to fetch Sonarr dates — falling back to Plex",
                    exc_info=True,
                )

        if self._dates:
            logger.info("Cached %d download dates from Radarr/Sonarr", len(self._dates))
