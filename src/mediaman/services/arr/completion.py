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
from collections.abc import Callable
from typing import Any, NotRequired, TypedDict, cast

from mediaman.services.arr.fetcher import ArrCard
from mediaman.services.arr.state import series_has_files
from mediaman.services.downloads.download_format import extract_poster_url

logger = logging.getLogger("mediaman")


class RecentDownloadItem(TypedDict):
    """A recent download row returned by :func:`fetch_and_sync_recent_downloads`.

    Matches the shape stored in ``recent_downloads`` and returned to callers
    such as :func:`~mediaman.services.downloads.download_queue.build_downloads_response`.
    """

    id: str  # dl_id — the unique download identifier
    title: str
    media_type: str
    poster_url: str
    completed_at: str


class CompletedItem(TypedDict):
    """A download item that has disappeared from the queue (i.e. completed)."""

    dl_id: str
    title: str
    media_type: str
    poster_url: str
    #: Optional TMDB ID added by callers that enrich the list before passing
    #: it to :func:`record_verified_completions`.  Not set by
    #: :func:`detect_completed` itself.
    tmdb_id: NotRequired[int | None]


def detect_completed(
    previous: dict[str, ArrCard] | dict[str, dict[str, Any]],
    current: dict[str, ArrCard] | dict[str, dict[str, Any]],
) -> list[CompletedItem]:
    """Find items that disappeared from the queue (i.e. completed).

    Returns a list of dicts with dl_id, title, media_type, poster_url,
    and (when present on the previous-snapshot entry) tmdb_id, ready
    for insertion into recent_downloads.

    The ``tmdb_id`` field on each :class:`CompletedItem` is what
    :func:`record_verified_completions` uses to disambiguate two
    same-titled releases.  Callers that build the snapshot map should
    therefore enrich each entry with ``tmdb_id`` before stashing it —
    see ``download_queue/__init__.py`` for the canonical enrichment.
    The :class:`ArrCard` payload accepted here is duck-typed, so any
    dict carrying ``title``, ``kind``, ``poster_url`` and (optionally)
    ``tmdb_id`` works.
    """
    completed: list[CompletedItem] = []
    for dl_id, item in previous.items():
        if dl_id not in current:
            entry: CompletedItem = cast(
                CompletedItem,
                {
                    "dl_id": dl_id,
                    "title": item.get("title", ""),
                    "media_type": item.get("kind", "movie"),
                    "poster_url": item.get("poster_url", ""),
                },
            )
            tmdb_id = item.get("tmdb_id")
            if isinstance(tmdb_id, int) and tmdb_id:
                entry["tmdb_id"] = tmdb_id
            completed.append(entry)
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
    completed: list[CompletedItem],
    build_client: Callable[[sqlite3.Connection, str], Any],
) -> None:
    """Record completed downloads in recent_downloads, skipping unverified items.

    An item is considered verified when Radarr/Sonarr confirms the title
    has a file / episode file. NZBGet-only items (those without the
    ``radarr:`` / ``sonarr:`` prefix) are treated as verified by default
    because there's no Arr source to cross-check against.

    Performance: the Radarr/Sonarr library is fetched once per call and
    indexed by ``{tmdbId: movie}`` / ``{tmdbId: series}`` so lookups are
    O(1) by ID.  A title-only fallback is used when no ``tmdb_id`` is
    present on the completed item (older entries, NZB-only grabs).  The
    fallback logs a WARNING because two same-titled releases would be
    silently merged on that path; once :func:`detect_completed` reliably
    propagates ``tmdb_id`` from the queue snapshot (D6 fix), the fallback
    becomes vanishingly rare.
    """
    # Fetch Radarr/Sonarr libraries once per call and index by tmdbId + title.
    # None signals "not yet fetched"; an empty dict means "fetched, nothing found".
    _radarr_by_id: dict[int, dict] | None = None
    _radarr_by_title: dict[str, dict] | None = None
    _sonarr_by_id: dict[int, dict] | None = None
    _sonarr_by_title: dict[str, dict] | None = None

    def _ensure_radarr() -> None:
        nonlocal _radarr_by_id, _radarr_by_title
        if _radarr_by_id is not None:
            return
        # Build indexes before marking as fetched — if get_movies() raises,
        # _radarr_by_id stays None so the next item retries (matching original behaviour).
        by_id: dict[int, dict] = {}
        by_title: dict[str, dict] = {}
        client = build_client(conn, "radarr")
        for m in client.get_movies() if client else []:
            if tid := m.get("tmdbId"):
                by_id[int(tid)] = m
            if t := (m.get("title") or ""):
                by_title[t] = m
        _radarr_by_id, _radarr_by_title = by_id, by_title

    def _ensure_sonarr() -> None:
        nonlocal _sonarr_by_id, _sonarr_by_title
        if _sonarr_by_id is not None:
            return
        by_id: dict[int, dict] = {}
        by_title: dict[str, dict] = {}
        client = build_client(conn, "sonarr")
        for s in client.get_series() if client else []:
            if tid := s.get("tmdbId"):
                by_id[int(tid)] = s
            if t := (s.get("title") or ""):
                by_title[t] = s
        _sonarr_by_id, _sonarr_by_title = by_id, by_title

    # Collect verified rows first; commit once at the end to avoid N fsyncs.
    to_insert: list[tuple] = []

    for c in completed:
        dl_id = c["dl_id"]
        title = c["title"]

        # Verify with Radarr/Sonarr that the item actually has files
        verified = False
        try:
            if dl_id.startswith("radarr:"):
                _ensure_radarr()
                assert _radarr_by_id is not None
                assert _radarr_by_title is not None
                tmdb_id = c.get("tmdb_id")
                movie = _radarr_by_id.get(int(tmdb_id)) if tmdb_id else None
                if movie is None:
                    if not tmdb_id:
                        logger.warning(
                            "record_verified_completions: no tmdb_id for %s "
                            "(title=%r) — falling back to title-only match; "
                            "two releases with the same title would be "
                            "indistinguishable",
                            dl_id,
                            title,
                        )
                    movie = _radarr_by_title.get(title)
                if movie is not None and bool(movie.get("hasFile")):
                    verified = True
            elif dl_id.startswith("sonarr:"):
                _ensure_sonarr()
                assert _sonarr_by_id is not None
                assert _sonarr_by_title is not None
                tmdb_id = c.get("tmdb_id")
                series = _sonarr_by_id.get(int(tmdb_id)) if tmdb_id else None
                if series is None:
                    if not tmdb_id:
                        logger.warning(
                            "record_verified_completions: no tmdb_id for %s "
                            "(title=%r) — falling back to title-only match; "
                            "two releases with the same title would be "
                            "indistinguishable",
                            dl_id,
                            title,
                        )
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
) -> list[RecentDownloadItem]:
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
    recent: list[RecentDownloadItem] = []
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
                    entries = client.get_movies() if service == "radarr" else client.get_series()
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
            service = (
                "radarr"
                if dl_id.startswith("radarr:")
                else ("sonarr" if dl_id.startswith("sonarr:") else "")
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

        recent.append(
            {
                "id": r["dl_id"],
                "title": r["title"],
                "media_type": r["media_type"],
                "poster_url": poster_url,
                "completed_at": r["completed_at"],
            }
        )

    return recent
