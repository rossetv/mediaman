"""Sonarr queue fetch and pack-aggregate logic."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import requests

from mediaman.services.arr.fetcher._base import (
    ArrCard,
    ArrEpisodeEntry,
    _iter_still_searching,
    clamp_progress,
    make_arr_card,
)
from mediaman.services.infra.http_client import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient
from mediaman.core.format import format_bytes
from mediaman.core.time import parse_iso_utc
from mediaman.services.downloads.download_format import (
    classify_series_upcoming,
    compute_series_released_at,
    extract_poster_url,
    format_episode_label,
)

# Cluster-key separator. Chosen as NUL (``\x00``) because it cannot
# legally appear in a downloadId, series title, or episode label coming
# back from Sonarr — the previous separator (``:``) collided when titles
# such as ``"Star Trek: Picard"`` appeared in the queue, silently merging
# two distinct rows into one cluster and double-counting pack totals.
_CLUSTER_SEP = "\x00"

logger = logging.getLogger("mediaman")


def _compute_cluster_keys(eps: list[ArrEpisodeEntry], card_series_id: int) -> list[str | None]:
    """Return a parallel list of cluster keys, one entry per episode in *eps*.

    A cluster key groups queue rows that represent the same NZB pack:

    * If ``downloadId`` is populated, it IS the cluster key.
    * If it's empty (Sonarr handoff, manually-added grab) we synthesise a
      stable key from ``seriesId + title + label`` so two distinct rows
      can't collapse into one and double-count pack totals (C19).
    * The synthesised key joins on :data:`_CLUSTER_SEP` (NUL) so a title
      literally containing ``:`` (the prior separator) can no longer
      collide.
    * If both title and label are empty the row is unaggregatable; we
      return ``None`` for that slot and log a warning.
    """
    keys: list[str | None] = []
    for e in eps:
        dl = e["download_id"] or ""
        if dl:
            keys.append(dl)
            continue
        title = e["title"] or ""
        label = e["label"] or ""
        if not title and not label:
            logger.warning(
                "arr_fetcher.refused_empty_dl series_id=%s — row missing "
                "downloadId and identifying metadata; skipping aggregation",
                card_series_id,
            )
            keys.append(None)
            continue
        keys.append(
            f"seriesId{_CLUSTER_SEP}{card_series_id}{_CLUSTER_SEP}{title}{_CLUSTER_SEP}{label}"
        )
    return keys


def _aggregate_totals_per_cluster(
    eps: list[ArrEpisodeEntry], cluster_keys: list[str | None]
) -> tuple[int, int, dict[str, int]]:
    """Aggregate ``size`` and ``sizeleft`` once per unique cluster key.

    Returns ``(total_size, total_left, cluster_counts)`` — the counts
    map each cluster key to the number of episodes sharing it, used by
    the pack-flag pass to mark every episode in a multi-episode pack.
    """
    cluster_counts: dict[str, int] = {}
    for k in cluster_keys:
        if k is not None:
            cluster_counts[k] = cluster_counts.get(k, 0) + 1

    seen_keys: set[str] = set()
    total_size = 0
    total_left = 0
    for e, k in zip(eps, cluster_keys, strict=False):
        if k is None or k in seen_keys:
            continue
        seen_keys.add(k)
        total_size += e["size"]
        total_left += e["sizeleft"]
    return total_size, total_left, cluster_counts


def _finalise_card_aggregates(
    card: ArrCard,
    eps: list[ArrEpisodeEntry],
    cluster_keys: list[str | None],
    cluster_counts: dict[str, int],
    total_size: int,
    total_left: int,
) -> None:
    """Write the final aggregate fields onto *card* and pack flags onto *eps*.

    Mutates both in place. The progress percentage is clamped to
    ``[0, 100]`` so a quirky ``sizeleft > size`` can't render a
    negative bar.
    """
    for e, k in zip(eps, cluster_keys, strict=False):
        e["is_pack_episode"] = k is not None and cluster_counts.get(k, 0) > 1

    downloading = sum(1 for e in eps if e["progress"] > 0)
    overall_pct = clamp_progress(total_size, total_left)
    card["episode_count"] = len(eps)
    card["downloading_count"] = downloading
    card["progress"] = overall_pct
    card["size"] = total_size
    card["sizeleft"] = total_left
    card["size_str"] = format_bytes(total_size)
    card["done_str"] = format_bytes(total_size - total_left)
    card["has_pack"] = any(e["is_pack_episode"] for e in eps)
    eps.sort(key=lambda e: e["label"])


def _aggregate_pack_episodes(card: ArrCard, card_series_id: int) -> None:
    """Compute pack-detection flags and size aggregates for a series card in place.

    A season can be a mix: some episodes arrive in a single NZB pack, some
    download individually, some are still searching. Pack episodes share one
    downloadId and each queue record reports the pack's totals — they have no
    meaningful per-episode progress. Flag each episode individually so the
    template can suppress useless mini-bars on pack rows while keeping them on
    individual rows.

    Implementation: three independently-testable passes — compute keys,
    aggregate totals once per unique key, write the final aggregates back
    onto the card.
    """
    eps = card["episodes"]
    cluster_keys = _compute_cluster_keys(eps, card_series_id)
    total_size, total_left, cluster_counts = _aggregate_totals_per_cluster(eps, cluster_keys)
    _finalise_card_aggregates(card, eps, cluster_keys, cluster_counts, total_size, total_left)


def fetch_sonarr_queue(client: ArrClient) -> list[ArrCard]:
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
        progress = clamp_progress(size, sizeleft)
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
            series_map[series_id] = make_arr_card(
                "series",
                s_title,
                source="Sonarr",
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
    # ``_iter_still_searching`` owns the outer try/except so a transient
    # upstream failure doesn't discard the queue cards we already
    # collected, and so both fetchers share a single exception-list
    # contract.
    queue_series_title_years = {
        (i["title"], i.get("year")) for i in items if i.get("kind") == "series"
    }
    for series in _iter_still_searching(client.get_series, service_label="Sonarr"):
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
            # Sonarr's episode endpoint propagates and discards every
            # already-collected card.
            logger.warning(
                "Failed to fetch episodes for Sonarr series %s",
                series_id,
                exc_info=True,
            )
            episodes_raw = []

        is_upcoming, release_label = classify_series_upcoming(series, episodes_raw)
        released_at = compute_series_released_at(episodes_raw)

        added_at = 0.0
        added_dt = parse_iso_utc(series.get("added", ""))
        if added_dt is not None:
            added_at = added_dt.timestamp()

        poster_url = extract_poster_url(series.get("images"))

        items.append(
            make_arr_card(
                "series",
                s_title,
                source="Sonarr",
                poster_url=poster_url,
                arr_id=series_id,
                title_slug=series.get("titleSlug", ""),
                added_at=added_at,
                released_at=released_at,
                is_upcoming=is_upcoming,
                release_label=release_label,
            )
        )
    return items
