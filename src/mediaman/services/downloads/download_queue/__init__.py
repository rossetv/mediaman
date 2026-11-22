"""Merged NZBGet + Radarr/Sonarr download queue response.

Orchestrates fetching, matching, completion detection and hero selection, delegating to
items (card builders, NZBGet matching, DownloadsResponse), classify (search hints,
countdown bands, deep links) and queue (NZBGet parsing, Arr matching, unmatched-NZB
accumulation).

The module-level state (_previous_queue, _previous_initialised, _state_lock) lives here
because the tests patch and assign those names on this package; keeping the state and
its mutators in the root avoids import-path workarounds. Public symbols remain
importable from here.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import cast

import requests

from mediaman.services.arr.base import ArrError
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
    parse_nzb_queue as _parse_nzb_queue,
)
from mediaman.services.downloads.nzbget import NzbgetError
from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)

__all__ = [
    "DownloadsResponse",
    "build_downloads_response",
    "build_episode_dicts",
    "build_nzbget_from_db",
    "fetch_arr_queue",
    "maybe_trigger_search",
    "nzb_matches_arr",
    "record_verified_completions",
]


# rationale: in-process snapshot of the previous NZBGet queue for completion
# detection; process restart resets it (reconcile-on-startup handles gaps);
# single-worker invariant.  _previous_initialised distinguishes "never polled"
# (startup — skip diff so a restart doesn't re-report every visible item as
# completed) from "polled at least once" (normal operation — diff is valid).
# Both fields are guarded by _state_lock below.
_previous_queue: dict[str, dict[str, object]] = {}
_previous_initialised: bool = False


# Lock guarding _previous_queue/_previous_initialised.
_state_lock = threading.Lock()


def _reset_previous_queue() -> None:
    global _previous_queue, _previous_initialised
    with _state_lock:
        _previous_queue = {}
        _previous_initialised = False


def _enrich_with_tmdb_ids(
    conn: sqlite3.Connection,
    current_map: dict[str, dict[str, object]],
    secret_key: str,
) -> None:
    """Stamp each Arr-sourced entry in *current_map* with its tmdb_id.

    record_verified_completions needs it to tell apart same-titled releases; otherwise
    the title-only fallback merges duplicates. The libraries are fetched lazily, only
    when an Arr-prefixed entry exists, and keyed by arr_id rather than the
    collision-prone title.

    Any exception is swallowed: a failure must not block completion detection, only
    narrow the disambiguation.
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
            radarr_client = _build_radarr(conn, secret_key)
            if radarr_client:
                for m in radarr_client.get_movies():
                    aid = m.get("id")
                    tid = m.get("tmdbId")
                    if isinstance(aid, int) and isinstance(tid, int):
                        radarr_tmdb_by_arr_id[aid] = tid
        except (SafeHTTPError, requests.RequestException, ArrError):
            logger.warning("tmdb-id enrichment: Radarr lookup failed", exc_info=True)

    if arr_ids_sonarr:
        try:
            sonarr_client = _build_sonarr(conn, secret_key)
            if sonarr_client:
                for s in sonarr_client.get_series():
                    aid = s.get("id")
                    tid = s.get("tmdbId")
                    if isinstance(aid, int) and isinstance(tid, int):
                        sonarr_tmdb_by_arr_id[aid] = tid
        except (SafeHTTPError, requests.RequestException, ArrError):
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


def _is_arr_entry(dl_id: str) -> bool:
    return dl_id.startswith(("radarr:", "sonarr:"))


def _maybe_record_completions(
    conn: sqlite3.Connection,
    current_map: dict[str, dict[str, object]],
    secret_key: str,
    *,
    nzb_fetch_failed: bool = False,
    matched_nzb_names: frozenset[str] = frozenset(),
) -> None:
    """Detect items that vanished since the last poll and record verified completions.

    The lock is held only to snapshot the previous queue and swap in the new one;
    Radarr/Sonarr I/O in record_verified_completions happens outside it, so a slow Arr
    cannot stall every /downloads request. Swapping first means a concurrent poll sees
    the new state and does not re-report the same completion.

    Arr-sourced entries get their tmdb_id before being stored, so the next call's
    detect_completed carries it through to verification instead of matching on the
    title.

    When *nzb_fetch_failed*, the NZBGet side of *current_map* is unknown rather than
    empty: only the Radarr/Sonarr entries are diffed, and the previous snapshot's
    NZB-only entries are carried into the stored one unchanged, to be diffed on the
    next poll that reaches NZBGet.

    An NZB-only id that vanished only because this poll matched its NZB to an Arr card
    (*matched_nzb_names*) is still downloading, so it is not reported as completed.
    """
    global _previous_queue, _previous_initialised

    _enrich_with_tmdb_ids(conn, current_map, secret_key)

    with _state_lock:
        previous_snapshot = _previous_queue
        previously_initialised = _previous_initialised
        if nzb_fetch_failed:
            carried = {k: v for k, v in previous_snapshot.items() if not _is_arr_entry(k)}
            _previous_queue = {**carried, **current_map}
        else:
            _previous_queue = current_map
        _previous_initialised = True

    if previously_initialised:
        if nzb_fetch_failed:
            previous_snapshot = {k: v for k, v in previous_snapshot.items() if _is_arr_entry(k)}
        completed = [
            c
            for c in detect_completed(previous_snapshot, current_map)
            if c["dl_id"] not in matched_nzb_names
        ]
        record_verified_completions(conn, completed, secret_key)


def _downloads_subtitle(
    hero: dict[str, object] | None,
    queue: list[dict[str, object]],
    upcoming: list[dict[str, object]],
) -> str:
    """Build the page-header subtitle, the single source for the server-rendered page and
    the poll-driven JS update.

    Something downloading gives "2 items in progress" (plus " · 3 coming soon" when
    releases are scheduled). Nothing downloading gives "Nothing downloading · 3 coming
    soon", or "" when nothing is scheduled either (the empty-state card says it all).
    """
    active = (1 if hero else 0) + len(queue)
    coming = len(upcoming)
    if active:
        line = f"{active} item{'s' if active != 1 else ''} in progress"
        if coming:
            line += f" · {coming} coming soon"
        return line
    if coming:
        return f"Nothing downloading · {coming} coming soon"
    return ""


def build_downloads_response(conn: sqlite3.Connection, secret_key: str) -> DownloadsResponse:
    """Build the download queue with hero selection, merging NZBGet and Radarr/Sonarr
    queues by fuzzy title match and adding recent downloads from the database.

    *secret_key* decrypts the stored API credentials. Returns {"hero", "queue",
    "upcoming", "recent", "subtitle"}.
    """
    from mediaman.services.downloads.download_format import select_hero

    # 1. Fetch *arr queue
    arr_items = fetch_arr_queue(conn, secret_key)
    arr_base_urls_map = _arr_base_urls(conn, secret_key)

    # 2. Fetch NZBGet queue + status
    nzb_client = build_nzbget_from_db(conn, secret_key)
    nzb_queue: list[dict[str, object]] = []
    nzb_status: dict[str, object] = {}
    nzb_fetch_failed = False

    if nzb_client:
        try:
            nzb_status = nzb_client.get_status()
            nzb_queue = nzb_client.get_queue()
        except (SafeHTTPError, requests.RequestException, NzbgetError):
            logger.warning("Failed to fetch NZBGet queue/status", exc_info=True)
            nzb_fetch_failed = True

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

    matched_nzb_names = frozenset(cast(str, nzb["dl_id"]) for nzb in nzb_parsed if nzb["_matched"])

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
    _maybe_record_completions(
        conn,
        current_map,
        secret_key,
        nzb_fetch_failed=nzb_fetch_failed,
        matched_nzb_names=matched_nzb_names,
    )

    # 7. Hero selection
    hero, queue = select_hero(items_as_dicts)

    # 8. Recent downloads (last 7 days), excluding anything actively in queue.
    active_ids = {cast(str, item["id"]) for item in items_as_dicts}
    active_titles = {cast(str, item["title"]) for item in items_as_dicts}
    recent = fetch_and_sync_recent_downloads(conn, active_ids, active_titles, secret_key)

    upcoming = cast(list[dict[str, object]], upcoming_items)
    return {
        "hero": hero,
        "queue": queue,
        "upcoming": upcoming,
        "recent": cast(list[dict[str, object]], recent),
        "subtitle": _downloads_subtitle(hero, queue, upcoming),
    }
