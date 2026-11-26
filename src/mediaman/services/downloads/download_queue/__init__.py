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
import time
from collections.abc import Callable, Collection
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import requests

from mediaman.core.time import now_utc
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import build_nzbget_from_db
from mediaman.services.arr.completion import (
    _reset_unchecked,
    carry_queued_since,
    detect_completed,
    fetch_and_sync_recent_downloads,
    record_verified_completions,
)
from mediaman.services.arr.fetcher import fetch_arr_queue_result
from mediaman.services.arr.fetcher._base import is_film_id
from mediaman.services.arr.search_trigger import maybe_trigger_search
from mediaman.services.arr.state import series_has_files, series_ready
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
from mediaman.services.infra import ConfigDecryptError, SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr._types import SonarrSeries
    from mediaman.services.arr.fetcher import ArrCard, FetchResult

logger = logging.getLogger(__name__)

__all__ = [
    "DownloadsResponse",
    "build_downloads_response",
    "build_episode_dicts",
    "build_nzbget_from_db",
    "downloads_subtitle",
    "fetch_arr_queue_result",
    "maybe_trigger_search",
    "nzb_matches_arr",
    "record_verified_completions",
]


# rationale: in-process snapshot of the previous NZBGet queue for completion
# detection; process restart resets it (reconcile-on-startup handles gaps);
# single-worker invariant.  _previous_initialised distinguishes "never polled"
# (startup — skip diff so a restart doesn't re-report every visible item as
# completed) from "polled at least once" (normal operation — diff is valid).
# All fields here are guarded by _state_lock below.
_previous_queue: dict[str, dict[str, object]] = {}
_previous_initialised: bool = False

# How long before it is first seen a download's file may be dated: an *arr dates a file before a
# slow import ends, and its clock may be behind. A file this new has only just arrived anyway.
_DATED_BEFORE_SEEN = timedelta(minutes=15)

# The cards each of Radarr and Sonarr last answered with, to know a download by while it is down.
_last_cards: list[ArrCard] = []

# What there is of each show by its title, as Sonarr last told, and until when that stands: an
# open page polls every two seconds, and need not read them each time. A read that fails is
# asked again sooner.
_READINESS_KEPT_SECONDS = 30.0
_READINESS_RETRY_SECONDS = 5.0
_show_readiness: dict[str, str] = {}
_readiness_kept_until: float | None = None

# The top card's id, which stays on top while it is coming in.
_last_hero = ""

_state_lock = threading.Lock()

# What a viewer is told while a service refuses its key or password, which only an admin can
# put right.
VIEWER_OUTAGE = "Downloads aren't available"


def _reset_previous_queue() -> None:
    global \
        _previous_queue, \
        _previous_initialised, \
        _last_cards, \
        _show_readiness, \
        _readiness_kept_until, \
        _last_hero
    with _state_lock:
        _previous_queue = {}
        _previous_initialised = False
        _last_cards = []
        _show_readiness = {}
        _readiness_kept_until = None
        _last_hero = ""
    _reset_unchecked()


def _remembered_cards(arr_result: FetchResult) -> list[ArrCard]:
    """Keep this poll's cards, and return those of the services that did not answer it."""
    global _last_cards
    listed = {card["dl_id"] for card in arr_result.cards}
    with _state_lock:
        remembered = [
            card
            for card in _last_cards
            if _service_of(card["dl_id"]) in arr_result.unreachable and card["dl_id"] not in listed
        ]
        _last_cards = [*arr_result.cards, *remembered]
    return remembered


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


def _service_of(dl_id: str) -> str:
    return {"radarr": "Radarr", "sonarr": "Sonarr"}.get(dl_id.partition(":")[0], "NZBGet")


def _maybe_record_completions(
    conn: sqlite3.Connection,
    current_map: dict[str, dict[str, object]],
    secret_key: str,
    *,
    unreachable: Collection[str] = (),
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

    What a service in *unreachable* holds is unknown rather than empty: the previous
    snapshot's entries of it that *current_map* lacks are carried into the stored one
    unchanged, to be diffed on the next poll that reaches the service.

    An NZB-only id that vanished only because this poll matched its NZB to an Arr card
    (*matched_nzb_names*) is still downloading, so it is not reported as completed.
    """
    global _previous_queue, _previous_initialised

    answered = {k: v for k, v in current_map.items() if _service_of(k) not in unreachable}
    _enrich_with_tmdb_ids(conn, answered, secret_key)

    with _state_lock:
        previous_snapshot = _previous_queue
        previously_initialised = _previous_initialised
        carried = {k: v for k, v in previous_snapshot.items() if _service_of(k) in unreachable}
        stored = {**carried, **current_map}
        carry_queued_since(previous_snapshot, stored, now_utc() - _DATED_BEFORE_SEEN)
        for dl_id, item in stored.items():
            before = previous_snapshot.get(dl_id, {})
            # The dl_id is made of the title, which another film may have taken over.
            same = item.get("arr_id") and item.get("arr_id") == before.get("arr_id")
            if same and before.get("tmdb_id") and "tmdb_id" not in item:
                item["tmdb_id"] = before["tmdb_id"]
        _previous_queue = stored
        _previous_initialised = True

    if previously_initialised:
        completed = [
            c
            for c in detect_completed(previous_snapshot, stored)
            if c["dl_id"] not in matched_nzb_names
        ]
        record_verified_completions(conn, completed, secret_key)


def _readiness_of(series: SonarrSeries) -> str:
    if series_ready(series):
        return "ready"
    return "part" if series_has_files(series) else "unknown"


def _read_show_readiness(
    conn: sqlite3.Connection, secret_key: str, unreachable: Collection[str]
) -> dict[str, str]:
    """Return what there is of each show, by its title; as last told while Sonarr does not
    answer. Two shows of one title that differ are unknown.
    """
    from mediaman.services.arr.build import build_sonarr_from_db

    global _show_readiness, _readiness_kept_until
    now = time.monotonic()
    with _state_lock:
        known, kept_until = _show_readiness, _readiness_kept_until
        if "Sonarr" in unreachable or (kept_until is not None and now < kept_until):
            return known
        # Held for the short wait while it is read, so a read that fails is soon asked again.
        _readiness_kept_until = now + _READINESS_RETRY_SECONDS
    try:
        client = build_sonarr_from_db(conn, secret_key)
        series = client.get_series() if client else []
    except (SafeHTTPError, requests.RequestException, ArrError):
        logger.warning("Failed to read which shows are complete", exc_info=True)
        return known
    read: dict[str, str] = {}
    for show in series:
        title, readiness = show.get("title") or "", _readiness_of(show)
        read[title] = readiness if read.get(title, readiness) == readiness else "unknown"
    with _state_lock:
        _show_readiness = read
        _readiness_kept_until = now + _READINESS_KEPT_SECONDS
    return read


def _mark_readiness(
    conn: sqlite3.Connection,
    recent: list[dict[str, object]],
    secret_key: str,
    unreachable: Collection[str],
) -> None:
    """Say of each recent item whether it is "ready", there in "part" (a show lacking episodes)
    or "unknown" (a show Sonarr has not told of, or has no file of).
    """
    shows = [
        item
        for item in recent
        if item["media_type"] == "series" and str(item["id"]).startswith("sonarr:")
    ]
    readiness = _read_show_readiness(conn, secret_key, unreachable) if shows else {}
    for item in recent:
        item["readiness"] = "ready"
    for item in shows:
        item["readiness"] = readiness.get(cast(str, item["title"]), "unknown")


def downloads_subtitle(active: int, coming: int, outage: str = "") -> str:
    """Build the page-header subtitle of *active* downloads in progress and *coming* ones
    scheduled. While the page holds rows of a service that does not answer, downloads.js
    counts them in itself, in the same words.

    Something downloading gives "2 items in progress" (plus " · 3 coming soon" when
    releases are scheduled). Nothing downloading gives "Nothing downloading · 3 coming
    soon", or "" when nothing is scheduled either (the empty-state card says it all).
    An *outage* is named in place of "Nothing downloading", which nobody can then tell.
    """
    if active:
        line = f"{active} item{'s' if active != 1 else ''} in progress"
        if coming:
            line += f" · {coming} coming soon"
        return f"{line} · {outage[0].lower()}{outage[1:]}" if outage else line
    if coming:
        return f"{outage or 'Nothing downloading'} · {coming} coming soon"
    return outage


def _names(services: list[str]) -> str:
    *rest, last = services
    return f"{', '.join(rest) + ' and ' if rest else ''}{last}"


def _outage_line(unreachable: list[str], refused: Collection[str] = ()) -> str:
    """Return "Can't reach Sonarr and NZBGet", "Can't sign in to Radarr", "Can't reach Sonarr or
    sign in to Radarr", or "" when everything answered. *refused* are those of *unreachable*
    that refused the key or password they were given: no fault of the network."""
    if not unreachable:
        return ""
    down = [name for name in unreachable if name not in refused]
    shut = [name for name in unreachable if name in refused]
    if not shut:
        return f"Can't reach {_names(down)}"
    if not down:
        return f"Can't sign in to {_names(shut)}"
    return f"Can't reach {_names(down)} or sign in to {_names(shut)}"


def _is_set_up(
    conn: sqlite3.Connection, secret_key: str, build: Callable[[sqlite3.Connection, str], object]
) -> bool:
    """Whether the service has an address and a key; one whose key cannot be read is set up,
    and named as not answering."""
    try:
        return build(conn, secret_key) is not None
    except ConfigDecryptError:
        return True


def build_downloads_response(conn: sqlite3.Connection, secret_key: str) -> DownloadsResponse:
    """Build the download queue with hero selection, merging NZBGet and Radarr/Sonarr
    queues by fuzzy title match and adding recent downloads from the database.

    *secret_key* decrypts the stored API credentials. Returns {"hero", "queue",
    "upcoming", "recent", "subtitle", "unreachable", "refused", "outage",
    "downloads_set_up"}: unreachable names the services that did not answer, refused those
    of them that refused their key, outage says so in a line, and downloads_set_up says
    whether Radarr ("movies") and Sonarr ("shows") have an address and a key.
    """
    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
    from mediaman.services.downloads.download_format import select_hero

    global _last_hero

    # 1. Fetch *arr queue
    arr_result = fetch_arr_queue_result(conn, secret_key)
    unreachable = list(arr_result.unreachable)
    refused = list(arr_result.refused)
    arr_base_urls_map = _arr_base_urls(conn, secret_key)

    # 2. Fetch NZBGet queue + status
    nzb_client = build_nzbget_from_db(conn, secret_key)
    nzb_queue: list[dict[str, object]] = []
    nzb_status: dict[str, object] = {}

    if nzb_client:
        try:
            nzb_status = nzb_client.get_status()
            nzb_queue = nzb_client.get_queue()
        except (SafeHTTPError, requests.RequestException, NzbgetError) as exc:
            logger.warning("Failed to fetch NZBGet queue/status", exc_info=True)
            unreachable.append("NZBGet")
            if isinstance(exc, SafeHTTPError) and exc.status_code in (401, 403):
                refused.append("NZBGet")

    raw_download_rate = nzb_status.get("DownloadRate", 0)
    download_rate = raw_download_rate if isinstance(raw_download_rate, int) else 0

    # 3. Parse NZBGet items.
    nzb_parsed = _parse_nzb_queue(nzb_queue)

    # 4. Match arr cards to NZBGet entries; collect upcoming separately.
    # Pass maybe_trigger_search explicitly so monkeypatching it at the
    # download_queue module level is correctly intercepted by tests.
    remembered = _remembered_cards(arr_result)
    items, upcoming_items = _build_arr_items(
        conn,
        arr_result.cards,
        nzb_parsed,
        arr_base_urls_map,
        download_rate,
        secret_key,
        trigger_search=maybe_trigger_search,
        remembered=remembered,
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
    # The upcoming too, for the poster of each that the app holds.
    by_id = {
        **current_map,
        **{i["id"]: cast(dict[str, object], i) for i in upcoming_items},
    }
    for card in [*arr_result.cards, *remembered]:
        if card["dl_id"] in by_id and "tmdb_id" in card:
            by_id[card["dl_id"]]["tmdb_id"] = card["tmdb_id"]
    _maybe_record_completions(
        conn,
        current_map,
        secret_key,
        unreachable=unreachable,
        matched_nzb_names=matched_nzb_names,
    )

    # 7. Hero selection
    with _state_lock:
        kept = _last_hero
    hero, queue = select_hero(items_as_dicts, kept)
    with _state_lock:
        _last_hero = cast(str, hero["id"]) if hero else ""

    # 8. Recent downloads (last 7 days), excluding anything actively in queue.
    active = {(cast(str, item["id"]), cast(str, item["title"])) for item in items_as_dicts}
    active_ids = {dl_id for dl_id, _ in active}
    # A film is told by its id: one of its title is not the one that arrived.
    active_titles = {title for i, title in active if not is_film_id(i, title)}
    recent = cast(
        list[dict[str, object]],
        fetch_and_sync_recent_downloads(conn, active_ids, active_titles, secret_key),
    )
    _mark_readiness(conn, recent, secret_key, unreachable)

    upcoming = cast(list[dict[str, object]], upcoming_items)
    outage = _outage_line(unreachable, refused)
    return {
        "hero": hero,
        "queue": queue,
        "upcoming": upcoming,
        "recent": recent,
        "subtitle": downloads_subtitle((1 if hero else 0) + len(queue), len(upcoming), outage),
        "unreachable": unreachable,
        "refused": refused,
        "outage": outage,
        "downloads_set_up": {
            "movies": _is_set_up(conn, secret_key, build_radarr_from_db),
            "shows": _is_set_up(conn, secret_key, build_sonarr_from_db),
        },
    }
