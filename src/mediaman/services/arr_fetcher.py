"""Fetch Radarr/Sonarr queue.

Public entry points:

- :func:`fetch_arr_queue` -- backward-compatible, returns a plain list of cards.
- :func:`fetch_arr_queue_result` -- returns a :class:`FetchResult` that also
  carries any fetch errors so the UI can display a banner.

NZBGet client construction lives in :func:`mediaman.services.arr_build.build_nzbget_from_db`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import TypedDict

from mediaman.services.download_format import (
    classify_movie_upcoming,
    classify_series_upcoming,
    fmt_episode_label,
    extract_poster_url,
)
from mediaman.services.format import format_bytes, parse_iso_utc

logger = logging.getLogger("mediaman")


@dataclass
class FetchResult:
    """Container returned by :func:`fetch_arr_queue_result`.

    ``cards`` is the list of download cards (same content as :func:`fetch_arr_queue`).
    ``errors`` is a list of human-readable error strings -- one per service that
    failed.  Empty when all fetches succeeded.  UI layers should surface these
    as a dismissible banner rather than hiding them silently.
    """

    cards: list = field(default_factory=list)
    errors: list = field(default_factory=list)


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


class BaseArrCard(TypedDict):
    """Fields guaranteed present on every download card."""

    kind: str        # 'movie' or 'series'
    dl_id: str
    title: str
    source: str      # 'Radarr' or 'Sonarr'
    poster_url: str


class ArrCard(BaseArrCard, total=False):
    """A download card produced by :func:`fetch_arr_queue`.

    Both movie and series cards share this shape; series cards additionally
    carry an ``episodes`` list.  ``total=False`` allows partial construction
    as cards are built up incrementally. The five fields in :class:`BaseArrCard`
    are always present; all others are optional.
    """

    year: int | None
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


def _make_radarr_card(
    title: str,
    *,
    year: int | None = None,
    poster_url: str = "",
    progress: int = 0,
    size: int = 0,
    sizeleft: int = 0,
    timeleft: str = "",
    status: str = "searching",
    is_upcoming: bool = False,
    release_label: str = "",
    arr_id: int = 0,
    title_slug: str = "",
    added_at: float = 0.0,
    release_names: list[str] | None = None,
) -> ArrCard:
    """Build a Radarr movie card with all required fields populated."""
    return ArrCard(
        kind="movie",
        dl_id="radarr:" + title,
        title=title,
        source="Radarr",
        poster_url=poster_url,
        year=year,
        progress=progress,
        size=size,
        sizeleft=sizeleft,
        size_str=format_bytes(size),
        done_str=format_bytes(size - sizeleft),
        timeleft=timeleft,
        status=status,
        is_upcoming=is_upcoming,
        release_label=release_label,
        arr_id=arr_id,
        title_slug=title_slug,
        added_at=added_at,
        release_names=release_names if release_names is not None else [],
    )


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
        items.append(_make_radarr_card(
            m_title,
            year=movie.get("year"),
            poster_url=poster_url,
            progress=progress,
            size=size,
            sizeleft=sizeleft,
            timeleft=q.get("timeleft") or "",
            status=status,
            release_names=[release_name] if release_name else [],
        ))
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
            added_dt = parse_iso_utc(movie.get("added", ""))
            if added_dt is not None:
                added_at = added_dt.timestamp()

            poster_url = extract_poster_url(movie.get("images")) or ""

            items.append(_make_radarr_card(
                m_title,
                poster_url=poster_url,
                arr_id=movie.get("id", 0),
                title_slug=movie.get("titleSlug", ""),
                added_at=added_at,
                is_upcoming=is_upcoming,
                release_label=release_label,
            ))
    except Exception:
        logger.warning("Failed to check Radarr for searching movies", exc_info=True)
    return items



def _make_sonarr_card(
    title: str,
    *,
    year: int | None = None,
    poster_url: str = "",
    episodes: list | None = None,
    episode_count: int = 0,
    downloading_count: int = 0,
    progress: int = 0,
    size: int = 0,
    sizeleft: int = 0,
    arr_id: int = 0,
    title_slug: str = "",
    added_at: float = 0.0,
    is_upcoming: bool = False,
    release_label: str = "",
    release_names: list | None = None,
) -> "ArrCard":
    """Build a Sonarr series card with all required fields populated."""
    return ArrCard(
        kind="series",
        dl_id="sonarr:" + title,
        title=title,
        source="Sonarr",
        poster_url=poster_url,
        year=year,
        episodes=episodes if episodes is not None else [],
        episode_count=episode_count,
        downloading_count=downloading_count,
        progress=progress,
        size=size,
        sizeleft=sizeleft,
        size_str=format_bytes(size),
        done_str=format_bytes(size - sizeleft),
        arr_id=arr_id,
        title_slug=title_slug,
        added_at=added_at,
        is_upcoming=is_upcoming,
        release_label=release_label,
        release_names=release_names if release_names is not None else [],
    )


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
            "size_str": format_bytes(size),
            "status": status,
            "download_id": q.get("downloadId", ""),
        }

        if series_id not in series_map:
            poster_url = extract_poster_url(series.get("images")) or ""
            s_title = series.get("title") or "Unknown"
            # Release filenames Sonarr grabbed — used to match NZBs
            # whose cleaned title differs from the series title
            # (localised names like "Sousou no Frieren" vs the
            # Sonarr title "Frieren: Beyond Journey's End").
            series_map[series_id] = _make_sonarr_card(
                s_title,
                year=series.get("year"),
                poster_url=poster_url,
            )

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
    for card_series_id, card in series_map.items():
        eps = card["episodes"]

        # Per-episode cluster key. We used to cluster by ``downloadId``
        # alone — but Sonarr occasionally emits queue rows with an empty
        # ``downloadId`` (during handoff, or for manually-added grabs),
        # and those rows all collapsed to the empty string key. If two
        # such rows happened to carry the same pack totals, the pack
        # aggregate double-counted them (C19).
        #
        # New rule: when ``downloadId`` is populated, cluster by it.
        # When it's empty, synthesise a stable key from the series +
        # title + season/episode coordinates so two distinct rows
        # can't collapse into one. If even that metadata is missing
        # (no title, no season, no episode), refuse to aggregate that
        # episode and log a warning — it contributes nothing to pack
        # totals rather than risk a double count.
        def _cluster_key(e: dict) -> str | None:
            dl = e.get("download_id", "") or ""
            if dl:
                return dl
            title = e.get("title", "") or ""
            label = e.get("label", "") or ""
            if not title and not label:
                logger.warning(
                    "arr_fetcher.refused_empty_dl series_id=%s — row missing "
                    "downloadId and identifying metadata; skipping aggregation",
                    card_series_id,
                )
                return None
            return f"seriesId:{card_series_id}:{title}:{label}"

        # Per-episode: pack if its cluster key is shared with another
        # episode in the same card.
        cluster_counts: dict[str, int] = {}
        cluster_keys: list[str | None] = []
        for e in eps:
            k = _cluster_key(e)
            cluster_keys.append(k)
            if k is not None:
                cluster_counts[k] = cluster_counts.get(k, 0) + 1
        for e, k in zip(eps, cluster_keys):
            e["is_pack_episode"] = k is not None and cluster_counts.get(k, 0) > 1

        # Aggregate by unique cluster key so pack totals aren't counted
        # once per episode.
        seen_keys: set[str] = set()
        total_size = 0
        total_left = 0
        for e, k in zip(eps, cluster_keys):
            if k is None:
                # Refused — don't contribute to aggregates.
                continue
            if k in seen_keys:
                continue
            seen_keys.add(k)
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
        card["size_str"] = format_bytes(total_size)
        card["done_str"] = format_bytes(total_size - total_left)
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
            added_dt = parse_iso_utc(series.get("added", ""))
            if added_dt is not None:
                added_at = added_dt.timestamp()

            poster_url = extract_poster_url(series.get("images")) or ""

            items.append(_make_sonarr_card(
                s_title,
                poster_url=poster_url,
                arr_id=series_id,
                title_slug=series.get("titleSlug", ""),
                added_at=added_at,
                is_upcoming=is_upcoming,
                release_label=release_label,
            ))
    except Exception:
        logger.warning("Failed to check Sonarr for searching series", exc_info=True)
    return items


def fetch_arr_queue_result(conn: sqlite3.Connection) -> FetchResult:
    """Fetch Radarr/Sonarr queues and return a :class:`FetchResult`.

    Unlike :func:`fetch_arr_queue`, this surfaces errors alongside cards so
    callers can show a UI banner when a service is unreachable.
    """
    from mediaman.config import load_config
    from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db

    config = load_config()
    result = FetchResult()

    try:
        radarr_client = build_radarr_from_db(conn, config.secret_key)
        if radarr_client is not None:
            result.cards.extend(_fetch_radarr_queue(radarr_client))
    except Exception as exc:
        msg = f"Radarr fetch failed: {exc}"
        logger.warning("Failed to fetch Radarr queue: %s", exc, exc_info=True)
        result.errors.append(msg)

    try:
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client is not None:
            result.cards.extend(_fetch_sonarr_queue(sonarr_client))
    except Exception as exc:
        msg = f"Sonarr fetch failed: {exc}"
        logger.warning("Failed to fetch Sonarr queue: %s", exc, exc_info=True)
        result.errors.append(msg)

    return result


def fetch_arr_queue(conn: sqlite3.Connection) -> list[ArrCard]:
    """Fetch Radarr/Sonarr queues, grouping Sonarr episodes by series.

    Returns a list of download cards.  Movies are one card each.
    TV series are grouped into a single card with an ``episodes`` list.

    This is the backward-compatible wrapper around :func:`fetch_arr_queue_result`.
    Callers that need to surface fetch errors to the UI should use that function
    instead.
    """
    return fetch_arr_queue_result(conn).cards
