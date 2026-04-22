"""Compute Radarr/Sonarr download state for a media item.

States:
- ``in_library`` — movie has a file, OR every aired season of the TV
  show has every episode downloaded.
- ``partial`` — TV only; at least one aired season has files but not
  all aired episodes are present.
- ``downloading`` — item is in the Arr download queue.
- ``queued`` — item is added to Radarr/Sonarr but has no files yet
  and is not in the queue.
- ``None`` — item is not tracked at all.
"""

from __future__ import annotations

from typing import TypedDict


class ArrCaches(TypedDict):
    radarr_movies: dict[int, dict]
    radarr_queue_tmdb_ids: set[int]
    sonarr_series: dict[int, dict]
    sonarr_queue_tmdb_ids: set[int]


def compute_download_state(media_type: str, tmdb_id: int, caches: ArrCaches) -> str | None:
    """Return the download state for an item, or ``None`` if untracked.

    Args:
        media_type: Either ``"movie"`` or ``"tv"``.
        tmdb_id: The TMDB identifier for the item.
        caches: Pre-fetched Radarr/Sonarr data keyed by TMDB ID.

    Returns:
        One of ``"in_library"``, ``"partial"``, ``"downloading"``,
        ``"queued"``, or ``None``.
    """
    if media_type == "movie":
        movie = caches["radarr_movies"].get(tmdb_id)
        if movie is None:
            return None
        if movie.get("hasFile"):
            return "in_library"
        if tmdb_id in caches["radarr_queue_tmdb_ids"]:
            return "downloading"
        return "queued"

    series = caches["sonarr_series"].get(tmdb_id)
    if series is None:
        return None

    # Only consider seasons that have aired (previousAiring is set) and
    # are not season 0 (specials).
    aired_seasons = [
        s for s in series.get("seasons", [])
        if s.get("seasonNumber", 0) > 0
        and (s.get("statistics") or {}).get("previousAiring")
    ]

    if aired_seasons:
        have_any = any(
            (s.get("statistics") or {}).get("episodeFileCount", 0) > 0
            for s in aired_seasons
        )
        have_all = all(
            (s.get("statistics") or {}).get("episodeFileCount", 0)
            >= (s.get("statistics") or {}).get("episodeCount", 0)
            and (s.get("statistics") or {}).get("episodeCount", 0) > 0
            for s in aired_seasons
        )
        if have_all:
            return "in_library"
        if have_any:
            return "partial"

    if tmdb_id in caches["sonarr_queue_tmdb_ids"]:
        return "downloading"
    return "queued"


def build_radarr_cache(client) -> ArrCaches:
    """Build the per-request Radarr cache fragment. Returns a partial
    ``ArrCaches`` containing only the Radarr keys; combine with
    ``build_sonarr_cache`` via dict-spread to get a full ``ArrCaches``.
    ``client`` may be ``None``."""
    if client is None:
        return {"radarr_movies": {}, "radarr_queue_tmdb_ids": set()}
    movies = {m.get("tmdbId"): m for m in client.get_movies() if m.get("tmdbId")}
    queue_ids = {
        (q.get("movie") or {}).get("tmdbId")
        for q in client.get_queue()
        if (q.get("movie") or {}).get("tmdbId")
    }
    return {"radarr_movies": movies, "radarr_queue_tmdb_ids": queue_ids}


def build_sonarr_cache(client) -> ArrCaches:
    """Build the per-request Sonarr cache fragment. Returns a partial
    ``ArrCaches`` containing only the Sonarr keys; combine with
    ``build_radarr_cache`` via dict-spread to get a full ``ArrCaches``.
    ``client`` may be ``None``."""
    if client is None:
        return {"sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}
    series = {s.get("tmdbId"): s for s in client.get_series() if s.get("tmdbId")}
    queue_ids = {
        (q.get("series") or {}).get("tmdbId")
        for q in client.get_queue()
        if (q.get("series") or {}).get("tmdbId")
    }
    return {"sonarr_series": series, "sonarr_queue_tmdb_ids": queue_ids}
