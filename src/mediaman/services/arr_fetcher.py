"""Fetch Radarr/Sonarr queue.

Public entry point: :func:`fetch_arr_queue`.
NZBGet client construction lives in :func:`mediaman.services.arr_build.build_nzbget_from_db`.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TypedDict

from mediaman.services.download_format import (
    classify_movie_upcoming,
    classify_series_upcoming,
    fmt_bytes,
    fmt_episode_label,
    parse_iso,
    extract_poster_url,
)

logger = logging.getLogger("mediaman")


class ArrEpisodeEntry(TypedDict, total=False):
    """A single episode entry within an :class:`ArrCard` (series cards only)."""

    label: str
    title: str
    progress: int
    size: int
    sizeleft: int
    size_str: str
    status: str
    download_id: str
    is_pack_episode: bool


class ArrCard(TypedDict, total=False):
    """A download card produced by :func:`fetch_arr_queue`.

    Both movie and series cards share this shape; series cards additionally
    carry an ``episodes`` list.  ``total=False`` allows partial construction
    as cards are built up incrementally.
    """

    kind: str            # 'movie' or 'series'
    dl_id: str
    title: str
    year: int | None
    source: str          # 'Radarr' or 'Sonarr'
    poster_url: str
    progress: int
    size: int
    sizeleft: int
    size_str: str
    done_str: str
    timeleft: str
    status: str
    is_upcoming: bool
    release_label: str
    arr_id: int
    title_slug: str
    added_at: float
    release_names: list[str]
    # Series-only fields
    episodes: list[ArrEpisodeEntry]
    episode_count: int
    downloading_count: int
    has_pack: bool


def _fetch_radarr_queue(client) -> list[ArrCard]:
    """Build Radarr download cards from an already-constructed client.

    Returns cards for queue entries plus monitored movies still searching.
    The inner loop over ``get_movies()`` keeps its own try/except so a
    failure there doesn't wipe out the queue entries we already have.
    """
    items: list[ArrCard] = []
    for q in client.get_queue():
        movie = q.get("movie") or {}
        size = q.get("size") or 0
        sizeleft = q.get("sizeleft") or 0
        progress = (
            round((1 - sizeleft / max(size, 1)) * 100) if size else 0
        )
        status = (
            q.get("status")
            or q.get("trackedDownloadStatus")
            or "queued"
        )
        poster_url = extract_poster_url(movie.get("images")) or ""
        m_title = (
            movie.get("title") or q.get("title") or "Unknown"
        )
        release_name = q.get("title") or ""
        items.append(
            {
                "kind": "movie",
                "dl_id": "radarr:" + m_title,
                "title": m_title,
                "year": movie.get("year"),
                "source": "Radarr",
                "poster_url": poster_url,
                "progress": progress,
                "size": size,
                "sizeleft": sizeleft,
                "size_str": fmt_bytes(size),
                "done_str": fmt_bytes(size - sizeleft),
                "timeleft": q.get("timeleft") or "",
                "status": status,
                "is_upcoming": False,
                "release_label": "",
                "arr_id": 0,
                "added_at": 0.0,
                "release_names": [release_name] if release_name else [],
            }
        )
    # Also include monitored movies still searching (not yet in queue).
    # Includes both released-but-stalled items and unreleased items.
    # Use (title, year) as the dedup key so same-title remakes don't
    # collide (e.g. "Dune" 1984 vs 2021).
    queue_title_years = {(i["title"], i.get("year")) for i in items if i.get("kind") == "movie"}
    try:
        for movie in client.get_movies():
            m_title = movie.get("title", "")
            m_year = movie.get("year")
            if not movie.get("monitored"):
                continue
            if movie.get("hasFile"):
                continue
            if (m_title, m_year) in queue_title_years:
                continue

            is_upcoming, release_label = classify_movie_upcoming(movie)

            # Parse added timestamp to epoch seconds (for search throttle)
            added_at = 0.0
            added_dt = parse_iso(movie.get("added", ""))
            if added_dt is not None:
                added_at = added_dt.timestamp()

            poster_url = extract_poster_url(movie.get("images")) or ""

            items.append({
                "kind": "movie",
                "dl_id": "radarr:" + m_title,
                "title": m_title,
                "source": "Radarr",
                "poster_url": poster_url,
                "progress": 0,
                "size": 0,
                "sizeleft": 0,
                "size_str": "0 B",
                "done_str": "0 B",
                "timeleft": "",
                "status": "searching",
                "arr_id": movie.get("id", 0),
                "title_slug": movie.get("titleSlug", ""),
                "added_at": added_at,
                "is_upcoming": is_upcoming,
                "release_label": release_label,
                "release_names": [],
            })
    except Exception:
        logger.warning("Failed to check Radarr for searching movies", exc_info=True)
    return items


def _fetch_sonarr_queue(client) -> list[ArrCard]:
    """Build Sonarr download cards from an already-constructed client.

    Groups queue episodes by series into one card each, then appends
    cards for monitored series still searching. The inner loop over
    ``get_series()`` keeps its own try/except for the same reason as
    :func:`_fetch_radarr_queue`.
    """
    items: list[ArrCard] = []
    series_map: dict[int, ArrCard] = {}  # series_id -> grouped card

    for q in client.get_queue():
        series = q.get("series") or {}
        episode = q.get("episode") or {}
        series_id = series.get("id", 0)
        size = q.get("size") or 0
        sizeleft = q.get("sizeleft") or 0
        progress = (
            round((1 - sizeleft / max(size, 1)) * 100) if size else 0
        )
        status = (
            q.get("status")
            or q.get("trackedDownloadStatus")
            or "queued"
        )

        season_num = episode.get("seasonNumber")
        ep_num = episode.get("episodeNumber")
        ep_label = fmt_episode_label(season_num, ep_num)

        ep_entry = {
            "label": ep_label,
            "title": episode.get("title", ""),
            "progress": progress,
            "size": size,
            "sizeleft": sizeleft,
            "size_str": fmt_bytes(size),
            "status": status,
            "download_id": q.get("downloadId", ""),
        }

        if series_id not in series_map:
            poster_url = extract_poster_url(series.get("images")) or ""
            s_title = series.get("title") or "Unknown"
            series_map[series_id] = {
                "kind": "series",
                "dl_id": "sonarr:" + s_title,
                "title": s_title,
                "year": series.get("year"),
                "source": "Sonarr",
                "poster_url": poster_url,
                "episodes": [],
                "is_upcoming": False,
                "release_label": "",
                "arr_id": 0,
                "added_at": 0.0,
                # Release filenames Sonarr grabbed — used to match NZBs
                # whose cleaned title differs from the series title
                # (localised names like "Sousou no Frieren" vs the
                # Sonarr title "Frieren: Beyond Journey's End").
                "release_names": [],
            }

        release_name = q.get("title") or ""
        if release_name:
            series_map[series_id]["release_names"].append(release_name)
        series_map[series_id]["episodes"].append(ep_entry)

    # Compute aggregates for each series. A season can be a mix:
    # some episodes arrive in a single NZB pack, some download
    # individually, some are still searching. Pack episodes share
    # one downloadId and each queue record reports the pack's
    # totals — they have no meaningful per-episode progress. Flag
    # each episode individually so the template can suppress
    # useless mini-bars on pack rows while keeping them on
    # individual rows.
    for card in series_map.values():
        eps = card["episodes"]

        # Per-episode: pack if its downloadId is shared with
        # another episode in the same card.
        dl_id_counts: dict[str, int] = {}
        for e in eps:
            dl = e.get("download_id", "")
            if dl:
                dl_id_counts[dl] = dl_id_counts.get(dl, 0) + 1
        for e in eps:
            dl = e.get("download_id", "")
            e["is_pack_episode"] = bool(dl) and dl_id_counts.get(dl, 0) > 1

        # Aggregate by unique downloadId so pack totals aren't
        # counted once per episode. Episodes with no downloadId
        # (shouldn't normally happen in the queue) contribute
        # their individual size/sizeleft.
        seen_ids: set[str] = set()
        total_size = 0
        total_left = 0
        for e in eps:
            dl = e.get("download_id", "")
            if dl:
                if dl in seen_ids:
                    continue
                seen_ids.add(dl)
            total_size += e["size"]
            total_left += e["sizeleft"]

        downloading = sum(1 for e in eps if e["progress"] > 0)
        overall_pct = (
            round((1 - total_left / max(total_size, 1)) * 100)
            if total_size
            else 0
        )
        card["episode_count"] = len(eps)
        card["downloading_count"] = downloading
        card["progress"] = overall_pct
        card["size"] = total_size
        card["sizeleft"] = total_left
        card["size_str"] = fmt_bytes(total_size)
        card["done_str"] = fmt_bytes(total_size - total_left)
        card["has_pack"] = any(e["is_pack_episode"] for e in eps)
        # Sort episodes by label
        eps.sort(key=lambda e: e["label"])
        items.append(card)

    # Also include monitored series still searching (not yet in queue).
    # Use (title, year) as the dedup key — some remakes share a title
    # (e.g. "The Office" UK vs US).
    queue_series_title_years = {(i["title"], i.get("year")) for i in items if i.get("kind") == "series"}
    try:
        for series in client.get_series():
            s_title = series.get("title", "")
            s_year = series.get("year")
            if not series.get("monitored"):
                continue
            stats = series.get("statistics") or {}
            if stats.get("episodeFileCount", 0) > 0:
                continue
            if (s_title, s_year) in queue_series_title_years:
                continue

            # Fetch episodes for upcoming classification
            series_id = series.get("id", 0)
            episodes_raw: list[dict] = []
            try:
                episodes_raw = client.get_episodes(series_id)
            except Exception:
                logger.warning(
                    "Failed to fetch episodes for Sonarr series %s",
                    series_id,
                    exc_info=True,
                )
                episodes_raw = []

            is_upcoming, release_label = classify_series_upcoming(
                series, episodes_raw
            )

            added_at = 0.0
            added_dt = parse_iso(series.get("added", ""))
            if added_dt is not None:
                added_at = added_dt.timestamp()

            poster_url = extract_poster_url(series.get("images")) or ""

            items.append({
                "kind": "series",
                "dl_id": "sonarr:" + s_title,
                "title": s_title,
                "source": "Sonarr",
                "poster_url": poster_url,
                "episodes": [],
                "episode_count": 0,
                "downloading_count": 0,
                "progress": 0,
                "size": 0,
                "sizeleft": 0,
                "size_str": "0 B",
                "done_str": "0 B",
                "arr_id": series_id,
                "title_slug": series.get("titleSlug", ""),
                "added_at": added_at,
                "is_upcoming": is_upcoming,
                "release_label": release_label,
                "release_names": [],
            })
    except Exception:
        logger.warning("Failed to check Sonarr for searching series", exc_info=True)
    return items


def fetch_arr_queue(conn: sqlite3.Connection) -> list[ArrCard]:
    """Fetch Radarr/Sonarr queues, grouping Sonarr episodes by series.

    Returns a list of download cards.  Movies are one card each.
    TV series are grouped into a single card with an ``episodes`` list.
    """
    from mediaman.config import load_config
    from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db

    config = load_config()
    items: list[ArrCard] = []
    try:
        radarr_client = build_radarr_from_db(conn, config.secret_key)
        if radarr_client is not None:
            items.extend(_fetch_radarr_queue(radarr_client))
    except Exception:
        logger.warning("Failed to fetch Radarr queue", exc_info=True)
    try:
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client is not None:
            items.extend(_fetch_sonarr_queue(sonarr_client))
    except Exception:
        logger.warning("Failed to fetch Sonarr queue", exc_info=True)
    return items
