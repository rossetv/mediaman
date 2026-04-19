"""Build the merged NZBGet + Radarr/Sonarr download queue response.

This module owns:

- The Radarr/Sonarr queue fetch, including enrichment for still-searching
  monitored items and upcoming classification.
- The NZBGet client factory.
- Auto-triggering Radarr/Sonarr searches for stalled items (throttled).
- Module-level mutable state for the "previous queue" snapshot used by
  completion detection, and the per-item search-trigger timestamps.

The only public entry point is :func:`build_downloads_response`, which
the downloads route (and the JSON API) delegate to.

Module-level globals
--------------------
- ``_previous_queue`` / ``_previous_initialised`` — last-poll snapshot
  used to detect completions. Reset between tests via
  :func:`_reset_previous_queue`.
- ``_last_search_trigger`` — maps ``dl_id`` to epoch seconds of the most
  recent auto-triggered search. Reset via :func:`_reset_search_triggers`.
- ``_state_lock`` — guards both of the above against races between a
  scheduler tick and an inbound HTTP request.

These live at module scope (rather than in a service class) because the
existing tests reset them directly; keeping them as globals avoids
touching those tests.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from mediaman.services.arr_completion import (
    _detect_completed,
    load_recent_downloads,
    record_verified_completions,
)
from mediaman.services.download_format import (
    _build_episode_summary,
    _build_item,
    _classify_movie_upcoming,
    _classify_series_upcoming,
    _fmt_bytes,
    _fmt_eta,
    _fmt_relative_time,
    _looks_like_series_nzb,
    _map_episode_state,
    _map_state,
    _normalise_for_match,
    _parse_clean_title,
    _parse_iso,
    _select_hero,
    extract_poster_url,
)

logger = logging.getLogger("mediaman")

# Module-level state for completion detection.
# Maps dl_id -> item dict from the previous poll.
_previous_queue: dict[str, dict] = {}
_previous_initialised: bool = False


def _reset_previous_queue() -> None:
    """Reset the in-memory queue snapshot. Used by tests."""
    global _previous_queue, _previous_initialised
    _previous_queue = {}
    _previous_initialised = False


# Module-level throttle for auto-triggered Radarr/Sonarr searches.
# Maps dl_id -> epoch seconds of last trigger.
_last_search_trigger: dict[str, float] = {}

# Parallel map: dl_id -> number of times we've triggered a search for this
# item since process start. Powers the "Searched N times" UI hint so users
# can see mediaman is actually poking Radarr/Sonarr rather than idling.
_search_count: dict[str, int] = {}

# Lock guarding _last_search_trigger, _search_count (in _maybe_trigger_search)
# and _previous_queue/_previous_initialised (in build_downloads_response).
_state_lock = threading.Lock()

_SEARCH_STALE_SECONDS = 5 * 60     # trigger if item has been searching > 5 min
_SEARCH_THROTTLE_SECONDS = 15 * 60  # don't re-trigger within 15 min


def _reset_search_triggers() -> None:
    """Clear the in-memory search-trigger snapshot. Used by tests."""
    _last_search_trigger.clear()
    _search_count.clear()


def _get_search_info(dl_id: str) -> tuple[int, float]:
    """Return ``(count, last_epoch_seconds)`` for a dl_id.

    ``(0, 0.0)`` means mediaman has never fired a search for this item
    (e.g. it's still within the 5-min staleness window, or the process
    was restarted since). Callers render this as "Added Xm ago" using
    the item's own added_at, rather than a misleading "Never searched".
    """
    with _state_lock:
        return _search_count.get(dl_id, 0), _last_search_trigger.get(dl_id, 0.0)


def _get_nzbget_client(conn: sqlite3.Connection):
    """Build NZBGet client from DB settings. Returns ``None`` if not configured."""
    from mediaman.config import load_config
    from mediaman.crypto import decrypt_value
    from mediaman.services.nzbget import NzbgetClient

    url_row = conn.execute(
        "SELECT value FROM settings WHERE key='nzbget_url'"
    ).fetchone()
    user_row = conn.execute(
        "SELECT value FROM settings WHERE key='nzbget_username'"
    ).fetchone()
    pass_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='nzbget_password'"
    ).fetchone()
    if not url_row or not user_row or not pass_row:
        return None
    password = pass_row["value"]
    if pass_row["encrypted"]:
        password = decrypt_value(password, load_config().secret_key, aad=b"nzbget_password")
    return NzbgetClient(url_row["value"], user_row["value"], password)


def _build_arr_client(conn: sqlite3.Connection, service: str):
    """Build a Radarr or Sonarr client from DB settings. Returns None if unconfigured."""
    from mediaman.config import load_config
    from mediaman.services.arr_build import (
        build_radarr_from_db,
        build_sonarr_from_db,
    )

    config = load_config()
    if service == "radarr":
        return build_radarr_from_db(conn, config.secret_key)
    if service == "sonarr":
        return build_sonarr_from_db(conn, config.secret_key)
    return None


def _maybe_trigger_search(
    conn: sqlite3.Connection, item: dict, matched_nzb: bool
) -> None:
    """Trigger a Radarr/Sonarr search for a stalled item, with throttling.

    Does nothing when:
    - item is upcoming (Radarr/Sonarr correctly won't search for it)
    - item is matched to an NZBGet entry (actively downloading)
    - item was added less than 5 minutes ago
    - a search was triggered for the same dl_id within the last 15 minutes
    """
    if item.get("is_upcoming"):
        return
    if matched_nzb:
        return
    arr_id = item.get("arr_id") or 0
    if not arr_id:
        return
    added_at = item.get("added_at") or 0.0
    now = time.time()
    if now - added_at < _SEARCH_STALE_SECONDS:
        return

    dl_id = item.get("dl_id") or ""

    with _state_lock:
        last = _last_search_trigger.get(dl_id, 0.0)
        if now - last < _SEARCH_THROTTLE_SECONDS:
            return

        try:
            if item.get("kind") == "movie":
                client = _build_arr_client(conn, "radarr")
                if client is None:
                    return
                client.search_movie(arr_id)
            elif item.get("kind") == "series":
                client = _build_arr_client(conn, "sonarr")
                if client is None:
                    return
                client.search_series(arr_id)
            else:
                return
            _last_search_trigger[dl_id] = now
            _search_count[dl_id] = _search_count.get(dl_id, 0) + 1
            logger.info("Triggered search for stalled item %s", dl_id)
        except Exception:
            logger.warning(
                "Failed to trigger search for %s", dl_id, exc_info=True
            )


def trigger_pending_searches(conn: sqlite3.Connection) -> None:
    """Poke Radarr/Sonarr to search for every monitored-but-missing item.

    Called from the APScheduler on a fixed interval so items don't sit in
    "searching" indefinitely when nobody's got the /downloads page open.

    Two passes:

    1. Iterate everything :func:`_get_arr_queue` surfaces — covers every
       Radarr movie with no file, and every Sonarr series with zero
       episode files.
    2. Hit Sonarr's ``wanted/missing`` endpoint to catch series that
       already have *some* episodes and are missing others — these are
       filtered out of pass 1 by the ``episodeFileCount > 0`` guard in
       :func:`_get_arr_queue`.

    Reuses the per-item throttle and ``arr_id == 0`` gate inside
    :func:`_maybe_trigger_search`, so already-queued items and
    recently-searched items are skipped automatically.
    """
    try:
        arr_items = _get_arr_queue(conn)
    except Exception:
        logger.warning("trigger_pending_searches: failed to fetch arr queue", exc_info=True)
        arr_items = []

    for item in arr_items:
        _maybe_trigger_search(conn, item, matched_nzb=False)

    try:
        _trigger_sonarr_partial_missing(conn, arr_items)
    except Exception:
        logger.warning(
            "trigger_pending_searches: sonarr partial-missing pass failed",
            exc_info=True,
        )


def _trigger_sonarr_partial_missing(
    conn: sqlite3.Connection, arr_items: list[dict]
) -> None:
    """Fire SeriesSearch for Sonarr series with partial missing episodes.

    Dedupes against series already handled by the main pass via
    ``arr_id``, and reuses the ``sonarr:{title}`` dl_id format so the
    per-item throttle recognises the same series across passes.
    """
    client = _build_arr_client(conn, "sonarr")
    if client is None:
        return

    already_poked = {
        item.get("arr_id")
        for item in arr_items
        if item.get("kind") == "series" and item.get("arr_id")
    }

    missing = client.get_missing_series()
    for series_id, title in missing.items():
        if series_id in already_poked:
            continue
        _maybe_trigger_search(
            conn,
            {
                "kind": "series",
                "dl_id": f"sonarr:{title}",
                "arr_id": series_id,
                "is_upcoming": False,
                "added_at": 0.0,
            },
            matched_nzb=False,
        )


def _get_arr_queue(conn: sqlite3.Connection) -> list[dict]:
    """Fetch Radarr/Sonarr queues, grouping Sonarr episodes by series.

    Returns a list of download cards.  Movies are one card each.
    TV series are grouped into a single card with an ``episodes`` list.
    """
    from mediaman.config import load_config
    from mediaman.services.settings_reader import get_string_setting

    config = load_config()
    items: list[dict] = []

    def _setting(key: str):
        return get_string_setting(conn, key, secret_key=config.secret_key)

    # Radarr queue — one card per movie
    radarr_url = _setting("radarr_url")
    radarr_key = _setting("radarr_api_key")
    if radarr_url and radarr_key:
        try:
            from mediaman.services.radarr import RadarrClient

            client = RadarrClient(radarr_url, radarr_key)
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
                        "size_str": _fmt_bytes(size),
                        "done_str": _fmt_bytes(size - sizeleft),
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

                    is_upcoming, release_label = _classify_movie_upcoming(movie)

                    # Parse added timestamp to epoch seconds (for search throttle)
                    added_at = 0.0
                    added_dt = _parse_iso(movie.get("added", ""))
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

        except Exception:
            logger.warning("Failed to fetch Radarr queue", exc_info=True)

    # Sonarr queue — group episodes by series
    sonarr_url = _setting("sonarr_url")
    sonarr_key = _setting("sonarr_api_key")
    if sonarr_url and sonarr_key:
        try:
            from mediaman.services.sonarr import SonarrClient

            client = SonarrClient(sonarr_url, sonarr_key)

            series_map: dict[int, dict] = {}  # series_id -> grouped card

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
                ep_label = ""
                if season_num is not None:
                    ep_label = f"S{season_num:02d}"
                    if ep_num is not None:
                        ep_label += f"E{ep_num:02d}"

                ep_entry = {
                    "label": ep_label,
                    "title": episode.get("title", ""),
                    "progress": progress,
                    "size": size,
                    "sizeleft": sizeleft,
                    "size_str": _fmt_bytes(size),
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
                card["size_str"] = _fmt_bytes(total_size)
                card["done_str"] = _fmt_bytes(total_size - total_left)
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

                    is_upcoming, release_label = _classify_series_upcoming(
                        series, episodes_raw
                    )

                    added_at = 0.0
                    added_dt = _parse_iso(series.get("added", ""))
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

        except Exception:
            logger.warning("Failed to fetch Sonarr queue", exc_info=True)

    return items


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
        logger.debug("Failed to load arr base URLs for deep links", exc_info=True)
        return {"radarr": "", "sonarr": ""}


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
        rel = _fmt_relative_time(last_search_ts, now)
        if not rel:
            return ""
        if search_count == 1:
            return f"Searched once · last attempt {rel}"
        return f"Searched {search_count}\u00d7 · last attempt {rel}"
    if added_at > 0:
        rel = _fmt_relative_time(added_at, now)
        if rel:
            return f"Added {rel} · waiting for first search"
    return ""


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


def build_downloads_response(conn: sqlite3.Connection) -> dict:
    """Build the simplified download queue with hero selection.

    Merges NZBGet + Radarr/Sonarr queues using fuzzy title matching,
    maps each item through ``_map_state`` / ``_build_item``, selects a
    hero, and fetches recent downloads from the database.

    Returns ``{"hero": dict|None, "queue": list[dict], "upcoming":
    list[dict], "recent": list[dict]}``.
    """
    # 1. Fetch *arr queue
    arr_items = _get_arr_queue(conn)
    arr_base_urls = _arr_base_urls(conn)

    # 2. Fetch NZBGet queue + status
    nzb_client = _get_nzbget_client(conn)
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
        clean = _parse_clean_title(nzb_name)
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
            "looks_like_series": _looks_like_series_nzb(nzb_name),
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
            upcoming_items.append(_build_item(
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
        arr_title_norm = _normalise_for_match(arr.get("title") or "")
        release_name_norms = [
            n for n in (
                _normalise_for_match(rn) for rn in (arr.get("release_names") or [])
            ) if n
        ]
        arr_candidates = [c for c in [arr_title_norm, *release_name_norms] if c]
        arr_is_series = arr.get("kind") == "series"
        matched_nzb = None

        def _nzb_matches_arr(nzb_t_norm: str) -> bool:
            """Return True if this NZB's normalised title matches any arr
            candidate — primary title or a Sonarr/Radarr release name."""
            for cand in arr_candidates:
                if cand in nzb_t_norm or nzb_t_norm in cand:
                    return True
            return False

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
                nzb_t_norm = _normalise_for_match(nzb.get("title") or "")
                if not nzb_t_norm:
                    continue
                if _nzb_matches_arr(nzb_t_norm):
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
                    nzb_t_norm = _normalise_for_match(nzb.get("title") or "")
                    if nzb_t_norm and _nzb_matches_arr(nzb_t_norm):
                        nzb["_matched"] = True
            state = _map_state(matched_nzb["raw_status"], has_nzbget_match=True)
            eta = _fmt_eta(matched_nzb["remain_mb"], download_rate)
            if state == "almost_ready":
                eta = "Post-processing\u2026"

            if arr.get("kind") == "series":
                eps_raw = arr.get("episodes", [])
                episodes = [
                    {
                        "label": e.get("label", ""),
                        "title": e.get("title", ""),
                        "state": _map_episode_state(e),
                        "progress": e.get("progress", 0),
                        "is_pack_episode": e.get("is_pack_episode", False),
                    }
                    for e in eps_raw
                ]
                episode_summary = _build_episode_summary(episodes)
                items.append(_build_item(
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
                items.append(_build_item(
                    dl_id=arr.get("dl_id", matched_nzb["dl_id"]),
                    title=arr.get("title") or matched_nzb["title"],
                    media_type="movie",
                    poster_url=arr.get("poster_url") or "",
                    state=state,
                    progress=matched_nzb["progress"],
                    eta=eta,
                    size_done=_fmt_bytes(matched_nzb["done_mb"] * 1024 * 1024),
                    size_total=_fmt_bytes(matched_nzb["file_mb"] * 1024 * 1024),
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
                eps_raw = arr.get("episodes", [])
                episodes = [
                    {
                        "label": e.get("label", ""),
                        "title": e.get("title", ""),
                        "state": _map_episode_state(e),
                        "progress": e.get("progress", 0),
                        "is_pack_episode": e.get("is_pack_episode", False),
                    }
                    for e in eps_raw
                ]
                episode_summary = _build_episode_summary(episodes)
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
                    state = _map_state(None, has_nzbget_match=False)
                search_count, last_search_ts = _get_search_info(arr.get("dl_id", ""))
                added_at = arr.get("added_at", 0.0)
                items.append(_build_item(
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
                    state = _map_state(None, has_nzbget_match=False)
                search_count, last_search_ts = _get_search_info(arr.get("dl_id", ""))
                added_at = arr.get("added_at", 0.0)
                items.append(_build_item(
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
            state = _map_state(nzb["raw_status"], has_nzbget_match=True)
            eta = _fmt_eta(nzb["remain_mb"], download_rate)
            if state == "almost_ready":
                eta = "Post-processing\u2026"
            media_type = "series" if nzb.get("looks_like_series") else "movie"
            items.append(_build_item(
                dl_id=nzb["dl_id"],
                title=nzb["title"],
                media_type=media_type,
                poster_url="",
                state=state,
                progress=nzb["progress"],
                eta=eta,
                size_done=_fmt_bytes(nzb["done_mb"] * 1024 * 1024),
                size_total=_fmt_bytes(nzb["file_mb"] * 1024 * 1024),
            ))

    # 6. Completion detection — items that vanished since the last poll.
    #    Only record as completed if Radarr/Sonarr confirms the item has files
    #    (prevents failed/removed grabs from appearing as "Ready to watch").
    current_map = {item["id"]: item for item in items}

    with _state_lock:
        global _previous_queue, _previous_initialised

        if _previous_initialised:
            completed = _detect_completed(_previous_queue, current_map)
            record_verified_completions(conn, completed, _build_arr_client)

        _previous_queue = current_map
        _previous_initialised = True

    # 7. Hero selection
    hero, queue = _select_hero(items)

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
