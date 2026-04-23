"""Build the merged NZBGet + Radarr/Sonarr download queue response.

This module is the thin orchestration layer.  The heavy lifting lives in:

- :mod:`mediaman.services.arr_fetcher` — Radarr/Sonarr queue fetch and NZBGet
  client construction.
- :mod:`mediaman.services.arr_search_trigger` — throttle state, reset helpers,
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
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import TypedDict

from mediaman.services.arr_completion import (
    _detect_completed,
    load_recent_downloads,
    record_verified_completions,
)
from mediaman.services.arr_build import build_arr_client as _build_arr_client, build_nzbget_from_db
from mediaman.services.arr_fetcher import fetch_arr_queue
from mediaman.services.arr_search_trigger import (
    get_search_info,
    _maybe_trigger_search,
    _reset_search_triggers,
    trigger_pending_searches,
)
from mediaman.services.download_format import (
    build_episode_summary,
    build_item,
    fmt_eta,
    fmt_relative_time,
    looks_like_series_nzb,
    map_episode_state,
    map_state,
    normalise_for_match,
    parse_clean_title,
    select_hero,
)
from mediaman.services.format import format_bytes

logger = logging.getLogger("mediaman")


class DownloadsResponse(TypedDict):
    """Return type for :func:`build_downloads_response`."""

    hero: dict | None
    queue: list
    upcoming: list
    recent: list


def _nzb_matches_arr(nzb_t_norm: str, arr_candidates: list[str]) -> bool:
    """Return True if *nzb_t_norm* matches any candidate in *arr_candidates*.

    Performs a bidirectional substring test so both "married at first sight
    au" ⊂ longer NZB titles and the reverse work correctly.  *arr_candidates*
    is a list of normalised strings built from the arr item's primary title
    and any release names Sonarr/Radarr recorded.
    """
    for cand in arr_candidates:
        if cand in nzb_t_norm or nzb_t_norm in cand:
            return True
    return False


def _build_episode_dicts(eps_raw: list[dict]) -> list[dict]:
    """Map raw Sonarr episode entries to simplified display dicts.

    Used in both the NZBGet-matched and unmatched branches so the two
    paths produce identical episode structures.
    """
    return [
        {
            "label": e.get("label", ""),
            "title": e.get("title", ""),
            "state": map_episode_state(e),
            "progress": e.get("progress", 0),
            "is_pack_episode": e.get("is_pack_episode", False),
        }
        for e in eps_raw
    ]


# Module-level state for completion detection.
# Maps dl_id -> item dict from the previous poll.
_previous_queue: dict[str, dict] = {}
_previous_initialised: bool = False


def _reset_previous_queue() -> None:
    """Reset the in-memory queue snapshot. Used by tests."""
    global _previous_queue, _previous_initialised
    _previous_queue = {}
    _previous_initialised = False


# Lock guarding _previous_queue/_previous_initialised.
_state_lock = threading.Lock()


def _build_search_hint(
    search_count: int,
    last_search_ts: float,
    added_at: float,
    now: float,
) -> str:
    """Build the "Last searched 12m ago" subline shown under the pill.

    Falls back to "Added Xm ago" when mediaman hasn't fired a search yet
    — either the item is still inside the 5-min staleness window, or the
    process was restarted so the in-memory trigger log is empty. Returns
    "" only when we genuinely have nothing to say.
    """
    if search_count > 0 and last_search_ts > 0:
        rel = fmt_relative_time(last_search_ts, now)
        if not rel:
            return ""
        if search_count == 1:
            return f"Searched once · last attempt {rel}"
        return f"Searched {search_count}\u00d7 · last attempt {rel}"
    if added_at > 0:
        rel = fmt_relative_time(added_at, now)
        if rel:
            return f"Added {rel} · waiting for first search"
    return ""


def _arr_base_urls(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{"radarr": url, "sonarr": url}`` for deep-link building.

    Prefers the **public** URL (``radarr_public_url``, ``sonarr_public_url``)
    when configured, because the value set in ``*_url`` is usually the
    in-cluster hostname (e.g. ``http://radarr:7878``) used by mediaman to
    reach the container directly — that URL is meaningless to a user's
    browser. Falls back to ``*_url`` when the public variant is empty
    so the default single-URL setup keeps working.

    Values have any trailing slash stripped. Missing settings (or a
    missing/invalid SECRET_KEY in a test fixture) map to ``""`` so
    callers can safely skip link rendering when the service isn't
    configured.
    """
    try:
        from mediaman.config import load_config
        from mediaman.services.settings_reader import get_string_setting

        secret_key = load_config().secret_key
        out = {}
        for service in ("radarr", "sonarr"):
            public = get_string_setting(
                conn, f"{service}_public_url", secret_key=secret_key,
            ) or ""
            internal = get_string_setting(
                conn, f"{service}_url", secret_key=secret_key,
            ) or ""
            chosen = public.strip() or internal.strip()
            out[service] = chosen.rstrip("/")
        return out
    except Exception:
        logger.warning("Failed to load arr base URLs for deep links", exc_info=True)
        return {"radarr": "", "sonarr": ""}


def _build_arr_link(arr: dict, base_urls: dict[str, str]) -> str:
    """Build a deep-link URL into Radarr/Sonarr for a stalled item.

    Returns ``""`` when the base URL isn't configured or the item has no
    title slug — we'd rather render nothing than a broken link.
    """
    slug = arr.get("title_slug") or ""
    if not slug:
        return ""
    kind = arr.get("kind")
    if kind == "movie" and base_urls.get("radarr"):
        return f"{base_urls['radarr'].rstrip('/')}/movie/{slug}"
    if kind == "series" and base_urls.get("sonarr"):
        return f"{base_urls['sonarr'].rstrip('/')}/series/{slug}"
    return ""


def _maybe_record_completions(conn: sqlite3.Connection, current_map: dict[str, dict]) -> None:
    """Detect items that vanished since the last poll and record verified completions.

    Mutates the module-level ``_previous_queue`` / ``_previous_initialised``
    snapshot under ``_state_lock``.  Separated from :func:`build_downloads_response`
    so that function only builds and returns data — it no longer has a DB-write
    side effect hidden inside a query function.
    """
    global _previous_queue, _previous_initialised

    with _state_lock:
        if _previous_initialised:
            completed = _detect_completed(_previous_queue, current_map)
            record_verified_completions(conn, completed, _build_arr_client)

        _previous_queue = current_map
        _previous_initialised = True


def build_downloads_response(conn: sqlite3.Connection) -> DownloadsResponse:
    """Build the simplified download queue with hero selection.

    Merges NZBGet + Radarr/Sonarr queues using fuzzy title matching,
    maps each item through ``map_state`` / ``build_item``, selects a
    hero, and fetches recent downloads from the database.

    Returns ``{"hero": dict|None, "queue": list[dict], "upcoming":
    list[dict], "recent": list[dict]}``.
    """
    # 1. Fetch *arr queue
    arr_items = fetch_arr_queue(conn)
    arr_base_urls = _arr_base_urls(conn)

    # 2. Fetch NZBGet queue + status
    nzb_client = build_nzbget_from_db(conn)
    nzb_queue: list[dict] = []
    nzb_status: dict = {}

    if nzb_client:
        try:
            nzb_status = nzb_client.get_status()
            nzb_queue = nzb_client.get_queue()
        except Exception:
            logger.warning("Failed to fetch NZBGet queue/status", exc_info=True)

    download_rate = nzb_status.get("DownloadRate", 0)

    # 3. Parse NZBGet items. We iterate the list directly rather than building
    #    a title->index dict: multiple episodes of the same series clean to the
    #    same title, so a dict would dedupe siblings and they'd leak through as
    #    unmatched "movie" items.
    nzb_parsed: list[dict] = []
    for nzb in nzb_queue:
        nzb_name = nzb.get("NZBName", "")
        clean = parse_clean_title(nzb_name)
        file_mb = nzb.get("FileSizeMB", 0)
        remain_mb = nzb.get("RemainingSizeMB", 0)
        done_mb = file_mb - remain_mb
        pct = round(done_mb / file_mb * 100) if file_mb > 0 else 0
        raw_status = nzb.get("Status", "")

        parsed = {
            "raw_status": raw_status,
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
        nzb_parsed.append(parsed)

    # 4. Match *arr items to NZBGet items, produce simplified items
    items: list[dict] = []
    upcoming_items: list[dict] = []

    for arr in arr_items:
        # Upcoming items bypass NZBGet matching entirely — Radarr/Sonarr won't
        # search them, so there will never be a matching NZBGet entry.
        if arr.get("is_upcoming"):
            upcoming_items.append(build_item(
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
            ))
            continue

        # Normalise for substring match so punctuation drift between the
        # *arr title ("Married at First Sight (AU)") and the cleaned NZB
        # name ("Married at First Sight AU") doesn't orphan the card.
        # Also match against release_names — filenames Sonarr/Radarr grabbed
        # from the indexer — so localised alt-titles ("Sousou no Frieren"
        # on the NZB vs "Frieren: Beyond Journey's End" on the arr side)
        # still group correctly.
        arr_title_norm = normalise_for_match(arr.get("title") or "")
        release_name_norms = [
            n for n in (
                normalise_for_match(rn) for rn in (arr.get("release_names") or [])
            ) if n
        ]
        arr_candidates = [c for c in [arr_title_norm, *release_name_norms] if c]
        arr_is_series = arr.get("kind") == "series"
        matched_nzb = None

        if arr_candidates:
            # Pick the least-complete matching NZB as the primary — for
            # series this gives a useful ETA (the episode that will
            # finish last), for movies there's usually only one match.
            best_remain = -1.0
            for nzb in nzb_parsed:
                if nzb["_matched"]:
                    continue
                # A movie-kind arr must not claim an NZB whose filename
                # carries a SxxExx marker — that's a TV episode, and letting
                # the substring title match ("the great" in "the greatest
                # showman") grab it would orphan the real series card.
                if not arr_is_series and nzb.get("looks_like_series"):
                    continue
                nzb_t_norm = normalise_for_match(nzb.get("title") or "")
                if not nzb_t_norm:
                    continue
                if _nzb_matches_arr(nzb_t_norm, arr_candidates):
                    remain = nzb.get("remain_mb", 0) or 0
                    if remain > best_remain:
                        best_remain = remain
                        matched_nzb = nzb

        if matched_nzb and not matched_nzb["_matched"]:
            matched_nzb["_matched"] = True
            # For a series, claim every other NZB whose cleaned title also
            # matches this series so sibling episodes don't fall through to
            # the unmatched-movie branch below. Without this, a second
            # episode of the same show renders as a poster-less movie card.
            if arr_is_series and arr_candidates:
                for nzb in nzb_parsed:
                    if nzb["_matched"]:
                        continue
                    nzb_t_norm = normalise_for_match(nzb.get("title") or "")
                    if nzb_t_norm and _nzb_matches_arr(nzb_t_norm, arr_candidates):
                        nzb["_matched"] = True
            state = map_state(matched_nzb["raw_status"], has_nzbget_match=True)
            eta = fmt_eta(matched_nzb["remain_mb"], download_rate)
            if state == "almost_ready":
                eta = "Post-processing\u2026"

            if arr.get("kind") == "series":
                episodes = _build_episode_dicts(arr.get("episodes", []))
                episode_summary = build_episode_summary(episodes)
                items.append(build_item(
                    dl_id=arr.get("dl_id", matched_nzb["dl_id"]),
                    title=arr.get("title") or matched_nzb["title"],
                    media_type="series",
                    poster_url=arr.get("poster_url") or "",
                    state=state,
                    progress=arr.get("progress", matched_nzb["progress"]),
                    eta=eta,
                    size_done=arr.get("done_str", ""),
                    size_total=arr.get("size_str", ""),
                    episodes=episodes,
                    episode_summary=episode_summary,
                    has_pack=arr.get("has_pack", False),
                ))
            else:
                items.append(build_item(
                    dl_id=arr.get("dl_id", matched_nzb["dl_id"]),
                    title=arr.get("title") or matched_nzb["title"],
                    media_type="movie",
                    poster_url=arr.get("poster_url") or "",
                    state=state,
                    progress=matched_nzb["progress"],
                    eta=eta,
                    size_done=format_bytes(matched_nzb["done_mb"] * 1024 * 1024),
                    size_total=format_bytes(matched_nzb["file_mb"] * 1024 * 1024),
                ))
            _maybe_trigger_search(conn, arr, matched_nzb=True)
        else:
            # *arr item with no NZBGet match. Default is "searching", but
            # episode-level progress often tells the real story: during
            # import Sonarr keeps the queue entry after NZBGet has cleared
            # the NZB, and with large queues the NZB-to-arr-title matcher
            # can miss a live download that Sonarr is genuinely tracking.
            # Derive the card state from the episodes when they exist so
            # users don't see "Looking for the best version" while a
            # progress bar is visibly advancing below it.
            if arr.get("kind") == "series":
                episodes = _build_episode_dicts(arr.get("episodes", []))
                episode_summary = build_episode_summary(episodes)
                if episodes and all(e["state"] == "ready" for e in episodes):
                    state = "almost_ready"
                elif any(
                    e["state"] in ("downloading", "queued") for e in episodes
                ):
                    # Something is actively or imminently downloading —
                    # user sees the series as in progress even if only one
                    # NZB is transferring right now.
                    state = "downloading"
                else:
                    state = map_state(None, has_nzbget_match=False)
                search_count, last_search_ts = get_search_info(arr.get("dl_id", ""))
                added_at = arr.get("added_at", 0.0)
                items.append(build_item(
                    dl_id=arr.get("dl_id", ""),
                    title=arr.get("title", "Unknown"),
                    media_type="series",
                    poster_url=arr.get("poster_url", ""),
                    state=state,
                    progress=arr.get("progress", 0),
                    eta="Post-processing\u2026" if state == "almost_ready" else "",
                    size_done=arr.get("done_str", ""),
                    size_total=arr.get("size_str", ""),
                    episodes=episodes,
                    episode_summary=episode_summary,
                    has_pack=arr.get("has_pack", False),
                    search_count=search_count,
                    last_search_ts=last_search_ts,
                    added_at=added_at,
                    search_hint=_build_search_hint(
                        search_count, last_search_ts, added_at, time.time()
                    ) if state == "searching" else "",
                    arr_link=_build_arr_link(arr, arr_base_urls),
                    arr_source=arr.get("source", ""),
                ))
            else:
                # Movie: same "arr done, NZB gone" transient — if Radarr
                # reports 100% we've almost_ready, not still searching.
                if (arr.get("progress") or 0) >= 100:
                    state = "almost_ready"
                else:
                    state = map_state(None, has_nzbget_match=False)
                search_count, last_search_ts = get_search_info(arr.get("dl_id", ""))
                added_at = arr.get("added_at", 0.0)
                items.append(build_item(
                    dl_id=arr.get("dl_id", ""),
                    title=arr.get("title", "Unknown"),
                    media_type="movie",
                    poster_url=arr.get("poster_url", ""),
                    state=state,
                    progress=arr.get("progress", 0),
                    eta="Post-processing\u2026" if state == "almost_ready" else "",
                    size_done=arr.get("done_str", "0 B"),
                    size_total=arr.get("size_str", "0 B"),
                    search_count=search_count,
                    last_search_ts=last_search_ts,
                    added_at=added_at,
                    search_hint=_build_search_hint(
                        search_count, last_search_ts, added_at, time.time()
                    ) if state == "searching" else "",
                    arr_link=_build_arr_link(arr, arr_base_urls),
                    arr_source=arr.get("source", ""),
                ))
            _maybe_trigger_search(conn, arr, matched_nzb=False)

    # 5. Add unmatched NZBGet items (manual additions with no Arr match).
    #    If the NZB filename carries a SxxExx marker, render as a series so
    #    the user isn't lied to by a "movie" pill on an obvious TV episode.
    for nzb in nzb_parsed:
        if not nzb["_matched"]:
            state = map_state(nzb["raw_status"], has_nzbget_match=True)
            eta = fmt_eta(nzb["remain_mb"], download_rate)
            if state == "almost_ready":
                eta = "Post-processing\u2026"
            media_type = "series" if nzb.get("looks_like_series") else "movie"
            items.append(build_item(
                dl_id=nzb["dl_id"],
                title=nzb["title"],
                media_type=media_type,
                poster_url="",
                state=state,
                progress=nzb["progress"],
                eta=eta,
                size_done=format_bytes(nzb["done_mb"] * 1024 * 1024),
                size_total=format_bytes(nzb["file_mb"] * 1024 * 1024),
            ))

    # 6. Completion detection — items that vanished since the last poll.
    #    Only record as completed if Radarr/Sonarr confirms the item has files
    #    (prevents failed/removed grabs from appearing as "Ready to watch").
    current_map = {item["id"]: item for item in items}
    _maybe_record_completions(conn, current_map)

    # 7. Hero selection
    hero, queue = select_hero(items)

    # 8. Recent downloads (last 7 days), excluding anything actively in queue.
    active_ids = {item["id"] for item in items}
    active_titles = {item["title"] for item in items}
    recent = load_recent_downloads(
        conn, active_ids, active_titles, _build_arr_client,
    )

    return {
        "hero": hero,
        "queue": queue,
        "upcoming": upcoming_items,
        "recent": recent,
    }
