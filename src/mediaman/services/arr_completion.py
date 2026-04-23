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
    """
    # Fetch Radarr/Sonarr libraries once per call and reuse across items.
    _radarr_movies: list[dict] | None = None
    _sonarr_series: list[dict] | None = None
    _radarr_built = False
    _sonarr_built = False

    def _get_radarr_movies() -> list[dict]:
        nonlocal _radarr_movies, _radarr_built
        if not _radarr_built:
            client = build_client(conn, "radarr")
            _radarr_movies = client.get_movies() if client else []
            _radarr_built = True
        return _radarr_movies or []

    def _get_sonarr_series() -> list[dict]:
        nonlocal _sonarr_series, _sonarr_built
        if not _sonarr_built:
            client = build_client(conn, "sonarr")
            _sonarr_series = client.get_series() if client else []
            _sonarr_built = True
        return _sonarr_series or []

    for c in completed:
        dl_id = c["dl_id"]
        title = c["title"]

        # Verify with Radarr/Sonarr that the item actually has files
        verified = False
        try:
            if dl_id.startswith("radarr:"):
                for movie in _get_radarr_movies():
                    if movie.get("title") == title and movie_has_file(movie):
                        verified = True
                        break
            elif dl_id.startswith("sonarr:"):
                for series in _get_sonarr_series():
                    if series.get("title") == title:
                        if series_has_files(series):
                            verified = True
                        break
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

        try:
            conn.execute(
                "INSERT OR IGNORE INTO recent_downloads "
                "(dl_id, title, media_type, poster_url) VALUES (?, ?, ?, ?)",
                (c["dl_id"], c["title"], c["media_type"], c["poster_url"]),
            )
            conn.commit()
        except Exception:
            logger.warning(
                "Failed to record completed download %s",
                dl_id,
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
