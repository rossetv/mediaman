"""download_queue package — builds the merged NZBGet + Radarr/Sonarr download queue response.

This module is the thin orchestration layer.  The heavy lifting lives in:

- :mod:`mediaman.services.arr.fetcher` — Radarr/Sonarr queue fetch and NZBGet
  client construction.
- :mod:`mediaman.services.arr.search_trigger` — throttle state, reset helpers,
  and the background :func:`trigger_pending_searches` job.

Module-level globals
--------------------
- ``_previous_queue`` / ``_previous_initialised`` — last-poll snapshot
  used to detect completions. Reset between tests via
  :func:`_reset_previous_queue`.
- ``_state_lock`` — guards the snapshot against races between a scheduler
  tick and an inbound HTTP request.

These live at module scope (rather than in a service class) because the
existing tests reset them directly; keeping them as globals avoids
touching those tests.

All previously-public symbols are re-exported here so existing imports such as
``from mediaman.services.downloads.download_queue import build_downloads_response``
continue to work without modification.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

from mediaman.services.arr.build import build_nzbget_from_db
from mediaman.services.arr.completion import (
    detect_completed,
    fetch_and_sync_recent_downloads,
    record_verified_completions,
)
from mediaman.services.arr.fetcher import fetch_arr_queue
from mediaman.services.arr.fetcher._base import ArrCard
from mediaman.services.arr.search_trigger import maybe_trigger_search
from mediaman.services.downloads.download_queue._deep_links import (
    arr_base_urls as _arr_base_urls,
)
from mediaman.services.downloads.download_queue._deep_links import (
    build_arr_link as _build_arr_link,
)
from mediaman.services.downloads.download_queue._deep_links import (
    build_search_hint as _build_search_hint,
)
from mediaman.services.downloads.download_queue._items import (
    build_episode_dicts,
    read_abandon_thresholds,
)
from mediaman.services.downloads.download_queue._items import (
    build_matched_item as _build_matched_item,
)
from mediaman.services.downloads.download_queue._items import (
    build_unmatched_arr_item as _build_unmatched_arr_item_impl,
)
from mediaman.services.downloads.download_queue._nzb_match import nzb_matches_arr
from mediaman.services.downloads.download_queue._response import DownloadsResponse

logger = logging.getLogger("mediaman")

__all__ = [
    "DownloadsResponse",
    "build_downloads_response",
    "build_episode_dicts",
    "nzb_matches_arr",
    "_reset_previous_queue",
    "_state_lock",
    "_previous_queue",
    "_previous_initialised",
]


# Module-level state for completion detection.
# Maps dl_id -> item dict from the previous poll.
_previous_queue: dict[str, dict[str, object]] = {}
_previous_initialised: bool = False


def _reset_previous_queue() -> None:
    """Reset the in-memory queue snapshot. Used by tests."""
    global _previous_queue, _previous_initialised
    _previous_queue = {}
    _previous_initialised = False


# Lock guarding _previous_queue/_previous_initialised.
_state_lock = threading.Lock()


def _maybe_record_completions(
    conn: sqlite3.Connection,
    current_map: dict[str, dict[str, object]],
    secret_key: str,
) -> None:
    """Detect items that vanished since the last poll and record verified completions.

    Lock discipline (C20): the lock is held only for the tiny critical
    section that snapshots the previous-queue state into local vars and
    then swaps in the new one. All HTTP I/O to Radarr/Sonarr (which
    ``record_verified_completions`` performs to verify an item has files
    before recording it) happens outside the lock — a slow/hung Arr
    must not stall every other thread waiting on ``_state_lock`` (and
    therefore every inbound ``/downloads`` request).

    The ordering here — swap the snapshot first, then do I/O — means a
    concurrent poll that arrives while we're still verifying will see
    the new state and not re-report the same completion. That's the
    right trade-off: the alternative (do I/O first, then swap) keeps
    the snapshot stale for the I/O window, which is worse.
    """
    from mediaman.services.arr.build import build_arr_client as _build_arr_client

    global _previous_queue, _previous_initialised

    with _state_lock:
        previous_snapshot = _previous_queue
        previously_initialised = _previous_initialised
        _previous_queue = current_map
        _previous_initialised = True

    if previously_initialised:
        completed = detect_completed(previous_snapshot, current_map)
        record_verified_completions(
            conn,
            completed,
            lambda c, svc: _build_arr_client(c, svc, secret_key),
        )


def _build_unmatched_arr_item(
    arr: "ArrCard",
    arr_base_urls_map: dict[str, str],
    abandon_thresholds: tuple[int, int],
) -> dict[str, object]:
    """Wrapper that binds the deep-link helpers + abandon thresholds for
    :func:`_build_unmatched_arr_item_impl`."""
    return _build_unmatched_arr_item_impl(
        arr,
        arr_base_urls_map,
        _build_search_hint,
        _build_arr_link,
        abandon_thresholds,
    )


def _parse_nzb_queue(
    nzb_queue: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Parse raw NZBGet queue entries into normalised dicts for matching.

    Each entry carries a ``_matched`` flag (initially False) so the
    arr-matching phase can claim entries without modifying the source list.
    """
    from mediaman.services.downloads.download_format import looks_like_series_nzb, parse_clean_title

    nzb_parsed: list[dict[str, object]] = []
    for nzb in nzb_queue:
        nzb_name = nzb.get("NZBName", "")
        clean = parse_clean_title(nzb_name)
        file_mb = nzb.get("FileSizeMB", 0)
        remain_mb = nzb.get("RemainingSizeMB", 0)
        done_mb = file_mb - remain_mb
        pct = round(done_mb / file_mb * 100) if file_mb > 0 else 0
        nzb_parsed.append(
            {
                "raw_status": nzb.get("Status", ""),
                "dl_id": nzb_name,
                "title": clean,
                "progress": pct,
                "file_mb": file_mb,
                "remain_mb": remain_mb,
                "done_mb": done_mb,
                "poster_url": "",
                "kind": "movie",
                "looks_like_series": looks_like_series_nzb(nzb_name),
                "_matched": False,
            }
        )
    return nzb_parsed


def _build_arr_items(
    conn: sqlite3.Connection,
    arr_items: list["ArrCard"],
    nzb_parsed: list[dict[str, object]],
    arr_base_urls_map: dict[str, str],
    download_rate: object,
    secret_key: str,
    abandon_thresholds: tuple[int, int],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Match arr cards to NZBGet entries and build simplified queue items.

    Returns ``(items, upcoming_items)``.  NZBGet entries that match an arr
    card are marked ``_matched=True`` in place so the caller can identify
    unmatched (manual) NZBGet entries afterwards.
    """
    from mediaman.services.downloads.download_format import (
        build_item,
        format_eta,
        map_state,
        normalise_for_match,
    )

    items: list[dict[str, object]] = []
    upcoming_items: list[dict[str, object]] = []

    for arr in arr_items:
        if arr.get("is_upcoming"):
            upcoming_items.append(
                build_item(
                    dl_id=arr.get("dl_id", ""),
                    title=arr.get("title", "Unknown"),
                    media_type="series" if arr.get("kind") == "series" else "movie",
                    poster_url=arr.get("poster_url", ""),
                    state="upcoming",
                    progress=0,
                    eta="",
                    size_done="",
                    size_total="",
                    release_label=arr.get("release_label", ""),
                    arr_id=arr.get("arr_id") or 0,
                    kind=arr.get("kind") or "",
                )
            )
            continue

        arr_title_norm = normalise_for_match(arr.get("title") or "")
        release_name_norms = [
            n for n in (normalise_for_match(rn) for rn in (arr.get("release_names") or [])) if n
        ]
        arr_candidates = [c for c in [arr_title_norm, *release_name_norms] if c]
        arr_is_series = arr.get("kind") == "series"
        matched_nzb = None

        if arr_candidates:
            best_remain = -1.0
            for nzb in nzb_parsed:
                if nzb["_matched"]:
                    continue
                if not arr_is_series and nzb.get("looks_like_series"):
                    continue
                nzb_t_norm = normalise_for_match(nzb.get("title") or "")
                if not nzb_t_norm:
                    continue
                if nzb_matches_arr(nzb_t_norm, arr_candidates):
                    remain = nzb.get("remain_mb", 0) or 0
                    if remain > best_remain:
                        best_remain = remain
                        matched_nzb = nzb

        if matched_nzb and not matched_nzb["_matched"]:
            matched_nzb["_matched"] = True
            if arr_is_series and arr_candidates:
                for nzb in nzb_parsed:
                    if nzb["_matched"]:
                        continue
                    nzb_t_norm = normalise_for_match(nzb.get("title") or "")
                    if nzb_t_norm and nzb_matches_arr(nzb_t_norm, arr_candidates):
                        nzb["_matched"] = True
            state = map_state(matched_nzb["raw_status"], has_nzbget_match=True)
            eta = format_eta(matched_nzb["remain_mb"], download_rate)
            if state == "almost_ready":
                eta = "Post-processing…"

            items.append(_build_matched_item(arr, matched_nzb, state, eta, download_rate))
            maybe_trigger_search(conn, arr, matched_nzb=True, secret_key=secret_key)
        else:
            items.append(_build_unmatched_arr_item(arr, arr_base_urls_map, abandon_thresholds))
            maybe_trigger_search(conn, arr, matched_nzb=False, secret_key=secret_key)

    return items, upcoming_items


def _add_unmatched_nzb_items(
    items: list[dict[str, object]],
    nzb_parsed: list[dict[str, object]],
    download_rate: object,
) -> None:
    """Append unmatched NZBGet entries (manual grabs with no arr card) to *items* in place."""
    from mediaman.services.downloads.download_format import build_item, format_eta, map_state
    from mediaman.services.infra.format import format_bytes

    for nzb in nzb_parsed:
        if nzb["_matched"]:
            continue
        state = map_state(nzb["raw_status"], has_nzbget_match=True)
        eta = format_eta(nzb["remain_mb"], download_rate)
        if state == "almost_ready":
            eta = "Post-processing…"
        media_type = "series" if nzb.get("looks_like_series") else "movie"
        items.append(
            build_item(
                dl_id=nzb["dl_id"],
                title=nzb["title"],
                media_type=media_type,
                poster_url="",
                state=state,
                progress=nzb["progress"],
                eta=eta,
                size_done=format_bytes(nzb["done_mb"] * 1024 * 1024),
                size_total=format_bytes(nzb["file_mb"] * 1024 * 1024),
                arr_id=0,
                kind=media_type,
            )
        )


def build_downloads_response(conn: sqlite3.Connection, secret_key: str) -> DownloadsResponse:
    """Build the simplified download queue with hero selection.

    Merges NZBGet + Radarr/Sonarr queues using fuzzy title matching,
    maps each item through ``map_state`` / ``build_item``, selects a
    hero, and fetches recent downloads from the database.

    ``secret_key`` is required for decrypting API credentials stored in DB
    settings (NZBGet password, Radarr/Sonarr API keys).

    Returns ``{"hero": dict|None, "queue": list[dict], "upcoming":
    list[dict], "recent": list[dict]}``.
    """
    from mediaman.services.downloads.download_format import select_hero

    # 1. Fetch *arr queue
    arr_items = fetch_arr_queue(conn, secret_key)
    arr_base_urls_map = _arr_base_urls(conn, secret_key)

    # 2. Fetch NZBGet queue + status
    nzb_client = build_nzbget_from_db(conn, secret_key)
    nzb_queue: list[dict[str, object]] = []
    nzb_status: dict[str, object] = {}

    if nzb_client:
        try:
            nzb_status = nzb_client.get_status()
            nzb_queue = nzb_client.get_queue()
        except Exception:
            logger.warning("Failed to fetch NZBGet queue/status", exc_info=True)

    download_rate = nzb_status.get("DownloadRate", 0)

    # 3. Parse NZBGet items.
    nzb_parsed = _parse_nzb_queue(nzb_queue)

    # 4. Read abandon thresholds once for this response cycle.
    abandon_thresholds = read_abandon_thresholds(conn)

    # 5. Match arr cards to NZBGet entries; collect upcoming separately.
    items, upcoming_items = _build_arr_items(
        conn,
        arr_items,
        nzb_parsed,
        arr_base_urls_map,
        download_rate,
        secret_key,
        abandon_thresholds,
    )

    # 6. Add unmatched NZBGet items (manual additions with no Arr match).
    _add_unmatched_nzb_items(items, nzb_parsed, download_rate)

    # 7. Completion detection.
    current_map = {item["id"]: item for item in items}
    _maybe_record_completions(conn, current_map, secret_key)

    # 8. Hero selection
    hero, queue = select_hero(items)

    # 9. Recent downloads (last 7 days), excluding anything actively in queue.
    from mediaman.services.arr.build import build_arr_client as _build_arr_client_local

    active_ids = {item["id"] for item in items}
    active_titles = {item["title"] for item in items}
    recent = fetch_and_sync_recent_downloads(
        conn,
        active_ids,
        active_titles,
        lambda c, svc: _build_arr_client_local(c, svc, secret_key),
    )

    return {
        "hero": hero,
        "queue": queue,
        "upcoming": upcoming_items,
        "recent": recent,
    }
