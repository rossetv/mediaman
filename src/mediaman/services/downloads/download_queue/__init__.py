"""download_queue package — builds the merged NZBGet + Radarr/Sonarr download queue response.

WHAT: Orchestrates fetching, matching, completion detection, and hero selection for the
      download queue. The heavy lifting is delegated to three focused submodules:

- :mod:`.items`    — item builders (matched/unmatched Arr entries), NZBGet matching,
                     episode helpers, and the ``DownloadsResponse`` TypedDict.
- :mod:`.classify` — state-derivation helpers: search hints, countdown bands,
                     deep links into Radarr/Sonarr.
- :mod:`.queue`    — stateless sub-functions: NZBGet parsing, Arr card matching,
                     unmatched-NZB accumulation.

WHY: The module-level state (``_previous_queue``, ``_previous_initialised``,
     ``_state_lock``) lives here — not in a submodule — because the test suite
     patches and directly assigns to these names via
     ``mediaman.services.downloads.download_queue._previous_queue`` etc. Keeping
     the state (and the functions that mutate it) in the package root avoids
     import-path gymnastics that would break those tests.

All previously-public symbols remain accessible here so existing imports such as
``from mediaman.services.downloads.download_queue import build_downloads_response``
continue to work without modification.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import cast

from mediaman.services.arr.build import build_nzbget_from_db
from mediaman.services.arr.completion import (
    detect_completed,
    fetch_and_sync_recent_downloads,
    record_verified_completions,
)
from mediaman.services.arr.fetcher import fetch_arr_queue
from mediaman.services.arr.search_trigger import maybe_trigger_search
from mediaman.services.downloads.download_queue.classify import (
    arr_base_urls as _arr_base_urls,
)
from mediaman.services.downloads.download_queue.items import (
    DownloadsResponse,
    build_episode_dicts,
    nzb_matches_arr,
)
from mediaman.services.downloads.download_queue.queue import (
    add_unmatched_nzb_items as _add_unmatched_nzb_items,
)
from mediaman.services.downloads.download_queue.queue import (
    build_arr_items as _build_arr_items,
)
from mediaman.services.downloads.download_queue.queue import (
    get_arr_base_urls as _get_arr_base_urls,
)
from mediaman.services.downloads.download_queue.queue import (
    parse_nzb_queue as _parse_nzb_queue,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DownloadsResponse",
    "_arr_base_urls",
    "_enrich_with_tmdb_ids",
    "_maybe_record_completions",
    "_previous_initialised",
    "_previous_queue",
    "_reset_previous_queue",
    "_state_lock",
    "build_downloads_response",
    "build_episode_dicts",
    "build_nzbget_from_db",
    "fetch_arr_queue",
    "maybe_trigger_search",
    "nzb_matches_arr",
    "record_verified_completions",
]


# ---------------------------------------------------------------------------
# Module-level state for completion detection.
# ---------------------------------------------------------------------------
# rationale: in-process snapshot of the previous NZBGet queue for completion
# detection; process restart resets it (reconcile-on-startup handles gaps);
# single-worker invariant.
_previous_queue: dict[str, dict[str, object]] = {}
_previous_initialised: bool = False


def _reset_previous_queue() -> None:
    """Reset the in-memory queue snapshot. Used by tests."""
    global _previous_queue, _previous_initialised
    _previous_queue = {}
    _previous_initialised = False


# Lock guarding _previous_queue/_previous_initialised.
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Completion detection helpers
# ---------------------------------------------------------------------------


def _enrich_with_tmdb_ids(
    conn: sqlite3.Connection,
    current_map: dict[str, dict[str, object]],
    secret_key: str,
) -> None:
    """Stamp each Arr-sourced entry in *current_map* with its ``tmdb_id``.

    The simplified items emitted by the queue builder don't carry
    ``tmdbId``, but :func:`record_verified_completions` needs it to
    disambiguate two same-titled releases (without it, the title-only
    fallback fires on every completion and silently merges duplicates).

    The enrichment fetches the Radarr/Sonarr libraries lazily — only
    when at least one Arr-prefixed entry is present — and builds an
    ``arr_id -> tmdb_id`` map. The ``arr_id`` is already on every
    queue item, so this keeps the lookup keyed off a stable, unique
    identifier rather than the (collision-prone) title.

    On any exception the enrichment silently bows out: a failure here
    must not block completion detection, only narrow the disambiguation
    window. ``record_verified_completions`` already logs a warning
    whenever the title-only fallback fires.
    """
    from mediaman.services.arr.build import build_radarr_from_db as _build_radarr
    from mediaman.services.arr.build import build_sonarr_from_db as _build_sonarr

    arr_ids_radarr = {
        v.get("arr_id")
        for v in current_map.values()
        if str(v.get("id", "")).startswith("radarr:") and v.get("arr_id")
    }
    arr_ids_sonarr = {
        v.get("arr_id")
        for v in current_map.values()
        if str(v.get("id", "")).startswith("sonarr:") and v.get("arr_id")
    }

    radarr_tmdb_by_arr_id: dict[int, int] = {}
    sonarr_tmdb_by_arr_id: dict[int, int] = {}

    if arr_ids_radarr:
        try:
            from mediaman.services.arr.base import ArrClient

            client = _build_radarr(conn, secret_key)
            if client:
                radarr_client = cast(ArrClient, client)
                for m in radarr_client.get_movies():
                    aid = m.get("id")
                    tid = m.get("tmdbId")
                    if isinstance(aid, int) and isinstance(tid, int):
                        radarr_tmdb_by_arr_id[aid] = tid
        except Exception:
            logger.warning("tmdb-id enrichment: Radarr lookup failed", exc_info=True)

    if arr_ids_sonarr:
        try:
            from mediaman.services.arr.base import ArrClient

            client = _build_sonarr(conn, secret_key)
            if client:
                sonarr_client = cast(ArrClient, client)
                for s in sonarr_client.get_series():
                    aid = s.get("id")
                    tid = s.get("tmdbId")
                    if isinstance(aid, int) and isinstance(tid, int):
                        sonarr_tmdb_by_arr_id[aid] = tid
        except Exception:
            logger.warning("tmdb-id enrichment: Sonarr lookup failed", exc_info=True)

    for v in current_map.values():
        dl_id = str(v.get("id", ""))
        arr_id = v.get("arr_id")
        if not isinstance(arr_id, int) or not arr_id:
            continue
        if dl_id.startswith("radarr:"):
            tid = radarr_tmdb_by_arr_id.get(arr_id)
            if tid:
                v["tmdb_id"] = tid
        elif dl_id.startswith("sonarr:"):
            tid = sonarr_tmdb_by_arr_id.get(arr_id)
            if tid:
                v["tmdb_id"] = tid


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

    Each Arr-sourced entry in ``current_map`` is enriched with its
    ``tmdb_id`` before being stashed into the previous-queue snapshot,
    so the next call's :func:`detect_completed` propagates the id all
    the way through to :func:`record_verified_completions` — pinning
    completion verification to a stable identifier instead of the
    collision-prone title.
    """
    global _previous_queue, _previous_initialised

    _enrich_with_tmdb_ids(conn, current_map, secret_key)

    with _state_lock:
        previous_snapshot = _previous_queue
        previously_initialised = _previous_initialised
        _previous_queue = current_map
        _previous_initialised = True

    if previously_initialised:
        completed = detect_completed(previous_snapshot, current_map)
        record_verified_completions(conn, completed, secret_key)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


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
    arr_base_urls_map = _get_arr_base_urls(conn, secret_key)

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

    raw_download_rate = nzb_status.get("DownloadRate", 0)
    download_rate = raw_download_rate if isinstance(raw_download_rate, int) else 0

    # 3. Parse NZBGet items.
    nzb_parsed = _parse_nzb_queue(nzb_queue)

    # 4. Match arr cards to NZBGet entries; collect upcoming separately.
    # Pass maybe_trigger_search explicitly so monkeypatching it at the
    # download_queue module level is correctly intercepted by tests.
    items, upcoming_items = _build_arr_items(
        conn,
        arr_items,
        nzb_parsed,
        arr_base_urls_map,
        download_rate,
        secret_key,
        trigger_search=maybe_trigger_search,
    )

    # 5. Add unmatched NZBGet items (manual additions with no Arr match).
    _add_unmatched_nzb_items(items, nzb_parsed, download_rate)

    # 6. Completion detection.
    # Cast through a plain-dict view because completion-detection helpers
    # were typed against ``dict[str, object]`` before ``DownloadItem`` was
    # introduced; the runtime shape is identical.
    items_as_dicts = cast(list[dict[str, object]], items)
    current_map: dict[str, dict[str, object]] = {
        cast(str, item["id"]): item for item in items_as_dicts
    }
    _maybe_record_completions(conn, current_map, secret_key)

    # 7. Hero selection
    hero, queue = select_hero(items_as_dicts)

    # 8. Recent downloads (last 7 days), excluding anything actively in queue.
    active_ids = {cast(str, item["id"]) for item in items_as_dicts}
    active_titles = {cast(str, item["title"]) for item in items_as_dicts}
    recent = fetch_and_sync_recent_downloads(conn, active_ids, active_titles, secret_key)

    return {
        "hero": hero,
        "queue": queue,
        "upcoming": cast(list[dict[str, object]], upcoming_items),
        "recent": cast(list[dict[str, object]], recent),
    }
