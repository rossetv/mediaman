"""Completion detection and recent-downloads persistence.

When an item disappears from the combined download queue between two
polls, we treat it as completed and record it in ``recent_downloads``
(subject to Radarr/Sonarr verification — a vanished item with no file
is probably a failed grab, not a finished one).

Kept separate from the queue builder so the pure completion logic
(``detect_completed``) can be unit-tested without any DB or HTTP
dependencies, and so the scheduler can import ``cleanup_recent_downloads``
without dragging in the whole queue module.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from mediaman.services.arr_fetcher import ArrCard
from mediaman.services.arr_state import movie_has_file, series_has_files
from mediaman.services.download_format import extract_poster_url

logger = logging.getLogger("mediaman")


def detect_completed(
    previous: dict[str, ArrCard], current: dict[str, ArrCard]
) -> list[dict]:
    """Find items that disappeared from the queue (i.e. completed).

    Returns a list of dicts with dl_id, title, media_type, poster_url
    ready for insertion into recent_downloads.
    """
    completed = []
    for dl_id, item in previous.items():
        if dl_id not in current:
            completed.append({
                "dl_id": dl_id,
                "title": item.get("title", ""),
                "media_type": item.get("kind", "movie"),
                "poster_url": item.get("poster_url", ""),
            })
    return completed


def cleanup_recent_downloads(conn: sqlite3.Connection) -> int:
    """Delete recent_downloads rows older than 7 days. Returns count deleted."""
    cursor = conn.execute(
        "DELETE FROM recent_downloads WHERE completed_at < datetime('now', '-7 days')"
    )
    conn.commit()
    return cursor.rowcount


def record_verified_completions(
    conn: sqlite3.Connection,
    completed: list[dict],
    build_client: Callable[[sqlite3.Connection, str], Any],
) -> None:
    """Record completed downloads in recent_downloads, skipping unverified items.

    An item is considered verified when Radarr/Sonarr confirms the title
    has a file / episode file. NZBGet-only items (those without the
    ``radarr:`` / ``sonarr:`` prefix) are treated as verified by default
    because there's no Arr source to cross-check against.

    Performance: the Radarr/Sonarr library is fetched once per call and
    indexed by ``{tmdbId: movie}`` / ``{tmdbId: series}`` so lookups are
    O(1) by ID.  A title-only fallback is used when no ``tmdbId`` is present
    on the completed item (older entries, NZB-only grabs).
    """
    # Fetch Radarr/Sonarr libraries once per call and index by tmdbId + title.
    _radarr_by_id: dict[int, dict] = {}
    _radarr_by_title: dict[str, dict] = {}
    _sonarr_by_id: dict[int, dict] = {}
    _sonarr_by_title: dict[str, dict] = {}
    _radarr_built = False
    _sonarr_built = False

    def _build_radarr() -> None:
        nonlocal _radarr_built
        if _radarr_built:
            return
        client = build_client(conn, "radarr")
        movies = client.get_movies() if client else []
        for m in movies:
            tid = m.get("tmdbId")
            if tid:
                _radarr_by_id[int(tid)] = m
            t = m.get("title") or ""
            if t:
                _radarr_by_title[t] = m
        _radarr_built = True

    def _build_sonarr() -> None:
        nonlocal _sonarr_built
        if _sonarr_built:
            return
        client = build_client(conn, "sonarr")
        series_list = client.get_series() if client else []
        for s in series_list:
            tid = s.get("tmdbId")
            if tid:
                _sonarr_by_id[int(tid)] = s
            t = s.get("title") or ""
            if t:
                _sonarr_by_title[t] = s
        _sonarr_built = True

    # Collect verified rows first; commit once at the end to avoid N fsyncs.
    to_insert: list[tuple] = []

    for c in completed:
        dl_id = c["dl_id"]
        title = c["title"]

        # Verify with Radarr/Sonarr that the item actually has files
        verified = False
        try:
            if dl_id.startswith("radarr:"):
                _build_radarr()
                tmdb_id = c.get("tmdb_id")
                movie = _radarr_by_id.get(int(tmdb_id)) if tmdb_id else None
                if movie is None:
                    movie = _radarr_by_title.get(title)
                if movie is not None and movie_has_file(movie):
                    verified = True
            elif dl_id.startswith("sonarr:"):
                _build_sonarr()
                tmdb_id = c.get("tmdb_id")
                series = _sonarr_by_id.get(int(tmdb_id)) if tmdb_id else None
                if series is None:
                    series = _sonarr_by_title.get(title)
                if series is not None and series_has_files(series):
                    verified = True
            else:
                # NZBGet-only items — no Arr verification possible
                verified = True
        except Exception:
            # Skip rather than log "no files confirmed" — the real cause is a network error
            logger.warning("Failed to verify completion for %s — skipping", dl_id, exc_info=True)
            continue

        if not verified:
            logger.info("Skipping completion for %s — no files confirmed", dl_id)
            continue

        to_insert.append((c["dl_id"], c["title"], c["media_type"], c["poster_url"]))

    # Single batch insert + single commit (D26 / per-row fsyncs finding).
    if to_insert:
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO recent_downloads "
                "(dl_id, title, media_type, poster_url) VALUES (?, ?, ?, ?)",
                to_insert,
            )
            conn.commit()
        except Exception:
            logger.warning(
                "Failed to record %d completed download(s)",
                len(to_insert),
                exc_info=True,
            )


def fetch_and_sync_recent_downloads(
    conn: sqlite3.Connection,
    active_ids: set[str],
    active_titles: set[str],
    build_client: Callable[[sqlite3.Connection, str], Any],
) -> list[dict]:
    """Return recent downloads (last 7 days), excluding anything active.

    Side effects: deletes rows whose item has reappeared in the active queue
    (e.g. Radarr re-grabbed after a bad release) and backfills missing poster
    URLs via Radarr/Sonarr.
    """
    recent_rows = conn.execute(
        "SELECT id, dl_id, title, media_type, poster_url, completed_at"
        " FROM recent_downloads"
        " WHERE completed_at >= datetime('now', '-7 days')"
        " ORDER BY completed_at DESC"
    ).fetchall()
    recent: list[dict] = []
    radarr_posters: dict[str, str] | None = None
    sonarr_posters: dict[str, str] | None = None

    def _poster_from_arr(service: str, title: str) -> str:
        """Lazy-load Radarr/Sonarr poster maps, look up *title*."""
        nonlocal radarr_posters, sonarr_posters
        cache = radarr_posters if service == "radarr" else sonarr_posters
        if cache is None:
            cache = {}
            try:
                client = build_client(conn, service)
                if client:
                    entries = (
                        client.get_movies() if service == "radarr"
                        else client.get_series()
                    )
                    for e in entries:
                        t = e.get("title") or ""
                        url = extract_poster_url(e.get("images"))
                        if url:
                            cache[t] = url
            except Exception:
                logger.warning(
                    "Failed to fetch %s posters for backfill",
                    service,
                    exc_info=True,
                )
            if service == "radarr":
                radarr_posters = cache
            else:
                sonarr_posters = cache
        return cache.get(title, "")

    for r in recent_rows:
        if r["dl_id"] in active_ids or r["title"] in active_titles:
            # Item is back in the download queue — remove from recent
            conn.execute("DELETE FROM recent_downloads WHERE id = ?", (r["id"],))
            conn.commit()
            continue

        poster_url = r["poster_url"] or ""
        if not poster_url:
            dl_id = r["dl_id"] or ""
            service = "radarr" if dl_id.startswith("radarr:") else (
                "sonarr" if dl_id.startswith("sonarr:") else ""
            )
            if not service:
                service = "radarr" if r["media_type"] == "movie" else "sonarr"
            poster_url = _poster_from_arr(service, r["title"])
            if poster_url:
                try:
                    conn.execute(
                        "UPDATE recent_downloads SET poster_url = ? WHERE id = ?",
                        (poster_url, r["id"]),
                    )
                    conn.commit()
                except Exception:
                    logger.warning(
                        "Failed to backfill poster for %s",
                        r["title"],
                        exc_info=True,
                    )

        recent.append({
            "id": r["dl_id"],
            "title": r["title"],
            "media_type": r["media_type"],
            "poster_url": poster_url,
            "completed_at": r["completed_at"],
        })

    return recent
