"""Sonarr queue fetch and pack-aggregate logic."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import requests

from mediaman.services.arr.fetcher._base import ArrCard, ArrEpisodeEntry, _format_size_fields
from mediaman.services.infra.http_client import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.sonarr import SonarrClient
from mediaman.services.downloads.download_format import (
    classify_series_upcoming,
    extract_poster_url,
    format_episode_label,
)
from mediaman.services.infra.format import format_bytes, parse_iso_utc

logger = logging.getLogger("mediaman")


def _make_sonarr_card(
    title: str,
    *,
    year: int | None = None,
    poster_url: str = "",
    episodes: list[ArrEpisodeEntry] | None = None,
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
    release_names: list[str] | None = None,
) -> ArrCard:
    """Build a Sonarr series card with all required fields populated."""
    size_str, done_str = _format_size_fields(size, sizeleft)
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
        size_str=size_str,
        done_str=done_str,
        arr_id=arr_id,
        title_slug=title_slug,
        added_at=added_at,
        is_upcoming=is_upcoming,
        release_label=release_label,
        release_names=release_names if release_names is not None else [],
    )


def _aggregate_pack_episodes(card: ArrCard, card_series_id: int) -> None:
    """Compute pack-detection flags and size aggregates for a series card in place.

    A season can be a mix: some episodes arrive in a single NZB pack, some
    download individually, some are still searching. Pack episodes share one
    downloadId and each queue record reports the pack's totals — they have no
    meaningful per-episode progress. Flag each episode individually so the
    template can suppress useless mini-bars on pack rows while keeping them on
    individual rows.
    """
    eps = card["episodes"]

    # Per-episode cluster key. We used to cluster by ``downloadId`` alone —
    # but Sonarr occasionally emits queue rows with an empty ``downloadId``
    # (during handoff, or for manually-added grabs), and those rows all
    # collapsed to the empty string key. If two such rows happened to carry
    # the same pack totals, the pack aggregate double-counted them (C19).
    #
    # New rule: when ``downloadId`` is populated, cluster by it. When it's
    # empty, synthesise a stable key from the series + title + season/episode
    # coordinates so two distinct rows can't collapse into one. If even that
    # metadata is missing (no title, no season, no episode), refuse to
    # aggregate that episode and log a warning.
    def _cluster_key(e: ArrEpisodeEntry) -> str | None:
        dl = e["download_id"] or ""
        if dl:
            return dl
        title = e["title"] or ""
        label = e["label"] or ""
        if not title and not label:
            logger.warning(
                "arr_fetcher.refused_empty_dl series_id=%s — row missing "
                "downloadId and identifying metadata; skipping aggregation",
                card_series_id,
            )
            return None
        return f"seriesId:{card_series_id}:{title}:{label}"

    cluster_counts: dict[str, int] = {}
    cluster_keys: list[str | None] = []
    for e in eps:
        k = _cluster_key(e)
        cluster_keys.append(k)
        if k is not None:
            cluster_counts[k] = cluster_counts.get(k, 0) + 1
    for e, k in zip(eps, cluster_keys):
        e["is_pack_episode"] = k is not None and cluster_counts.get(k, 0) > 1

    # Aggregate by unique cluster key so pack totals aren't counted once per episode.
    seen_keys: set[str] = set()
    total_size = 0
    total_left = 0
    for e, k in zip(eps, cluster_keys):
        if k is None:
            continue
        if k in seen_keys:
            continue
        seen_keys.add(k)
        total_size += e["size"]
        total_left += e["sizeleft"]

    downloading = sum(1 for e in eps if e["progress"] > 0)
    # Clamp the aggregate percentage too — once any constituent record
    # reports ``sizeleft > size`` the rolled-up subtraction can drop
    # below zero or punch above 100.
    overall_pct = (
        max(0, min(100, round((1 - total_left / max(total_size, 1)) * 100))) if total_size else 0
    )
    card["episode_count"] = len(eps)
    card["downloading_count"] = downloading
    card["progress"] = overall_pct
    card["size"] = total_size
    card["sizeleft"] = total_left
    card["size_str"] = format_bytes(total_size)
    card["done_str"] = format_bytes(total_size - total_left)
    card["has_pack"] = any(e["is_pack_episode"] for e in eps)
    eps.sort(key=lambda e: e["label"])


def fetch_sonarr_queue(client: SonarrClient) -> list[ArrCard]:
    """Build Sonarr download cards from an already-constructed client.

    Groups queue episodes by series into one card each, then appends
    cards for monitored series still searching. The inner loop over
    ``get_series()`` keeps its own try/except for the same reason as
    :func:`fetch_radarr_queue`.
    """
    items: list[ArrCard] = []
    series_map: dict[int, ArrCard] = {}  # series_id -> grouped card

    for q in client.get_queue():
        series = q.get("series") or {}
        episode = q.get("episode") or {}
        series_id = series.get("id", 0)
        size = q.get("size") or 0
        sizeleft = q.get("sizeleft") or 0
        # Clamp to [0, 100]: Sonarr can briefly report ``sizeleft > size``
        # while a torrent re-downloads, which would otherwise produce a
        # negative percentage and break progress bars.
        progress = max(0, min(100, round((1 - sizeleft / max(size, 1)) * 100))) if size else 0
        status = q.get("status") or q.get("trackedDownloadStatus") or "queued"

        season_num = episode.get("seasonNumber")
        ep_num = episode.get("episodeNumber")
        ep_label = format_episode_label(season_num, ep_num)

        ep_entry: ArrEpisodeEntry = cast(
            ArrEpisodeEntry,
            {
                "label": ep_label,
                "title": episode.get("title", ""),
                "progress": progress,
                "size": size,
                "sizeleft": sizeleft,
                "size_str": format_bytes(size),
                "status": status,
                "download_id": q.get("downloadId", ""),
                "season_number": int(season_num) if season_num is not None else 0,
            },
        )

        if series_id not in series_map:
            poster_url = extract_poster_url(series.get("images"))
            s_title = series.get("title") or "Unknown"
            series_map[series_id] = _make_sonarr_card(
                s_title,
                year=series.get("year"),
                poster_url=poster_url,
            )

        release_name = q.get("title") or ""
        if release_name:
            series_map[series_id]["release_names"].append(release_name)
        series_map[series_id]["episodes"].append(ep_entry)

    for card_series_id, card in series_map.items():
        _aggregate_pack_episodes(card, card_series_id)
        items.append(card)

    # Also include monitored series still searching (not yet in queue).
    queue_series_title_years = {
        (i["title"], i.get("year")) for i in items if i.get("kind") == "series"
    }
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

            series_id = series.get("id", 0)
            episodes_raw: list[dict] = []
            try:
                episodes_raw = client.get_episodes(series_id)
            except (requests.RequestException, SafeHTTPError):
                # ``SafeHTTPError`` for non-2xx responses must be caught
                # alongside ``RequestException``; otherwise a 503 from
                # Sonarr's episode endpoint propagates to the outer
                # handler and discards every already-collected card.
                logger.warning(
                    "Failed to fetch episodes for Sonarr series %s",
                    series_id,
                    exc_info=True,
                )
                episodes_raw = []

            is_upcoming, release_label = classify_series_upcoming(series, episodes_raw)

            added_at = 0.0
            added_dt = parse_iso_utc(series.get("added", ""))
            if added_dt is not None:
                added_at = added_dt.timestamp()

            poster_url = extract_poster_url(series.get("images"))

            items.append(
                _make_sonarr_card(
                    s_title,
                    poster_url=poster_url,
                    arr_id=series_id,
                    title_slug=series.get("titleSlug", ""),
                    added_at=added_at,
                    is_upcoming=is_upcoming,
                    release_label=release_label,
                )
            )
    except (requests.RequestException, SafeHTTPError):
        # See note above: ``SafeHTTPError`` is NOT a ``RequestException``
        # subclass, so without it a 503 from ``get_series`` would discard
        # every queue card we already collected.
        logger.warning("Failed to check Sonarr for searching series", exc_info=True)
    return items
