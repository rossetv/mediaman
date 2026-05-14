"""Completion detection and recent-downloads sync/persistence.

Owns:

* :func:`detect_completed` — pure diff of two queue snapshots.
* :func:`cleanup_recent_downloads` — TTL-based row purge.
* :class:`_PosterLookup` — lazy, call-scoped poster-map for backfill.
* :func:`_sync_recent_row` — per-row reappearance check + poster backfill.
* :func:`fetch_and_sync_recent_downloads` — the public read path.

The verification helpers (:class:`~._verification._ArrLibraryIndex`,
:func:`~._verification.record_verified_completions`, etc.) live in
:mod:`._verification`.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import cast

import requests

from mediaman.services.arr.base import ArrError
from mediaman.services.arr.completion._types import CompletedItem, RecentDownloadItem
from mediaman.services.arr.fetcher import ArrCard
from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)


def detect_completed(
    previous: Mapping[str, ArrCard] | Mapping[str, Mapping[str, object]],
    current: Mapping[str, ArrCard] | Mapping[str, Mapping[str, object]],
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


class _PosterLookup:
    """Lazy, call-scoped Radarr/Sonarr poster maps for recent-downloads backfill.

    Each service's library is fetched at most once per
    :func:`fetch_and_sync_recent_downloads` call and indexed
    ``{title: poster_url}``. ``None`` signals "not yet fetched"; an empty
    dict means "fetched, nothing usable found". Replaces the former
    ``_poster_from_arr`` nested closure — same lazy-load-and-look-up
    behaviour, hoisted so it is independently testable.
    """

    def __init__(self, conn: sqlite3.Connection, secret_key: str) -> None:
        self._conn = conn
        self._secret_key = secret_key
        self._radarr_posters: dict[str, str] | None = None
        self._sonarr_posters: dict[str, str] | None = None

    def poster_for(self, service: str, title: str) -> str:
        """Return the poster URL for *title* from *service*, lazy-loading the map."""
        from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db

        cache = self._radarr_posters if service == "radarr" else self._sonarr_posters
        if cache is None:
            cache = {}
            try:
                client = (
                    build_radarr_from_db(self._conn, self._secret_key)
                    if service == "radarr"
                    else build_sonarr_from_db(self._conn, self._secret_key)
                )
                if client:
                    entries = client.get_movies() if service == "radarr" else client.get_series()
                    for e in entries:
                        t = e.get("title") or ""
                        url = extract_poster_url(e.get("images"))
                        if url:
                            cache[t] = url
            except (SafeHTTPError, requests.RequestException, ArrError):
                logger.warning(
                    "Failed to fetch %s posters for backfill",
                    service,
                    exc_info=True,
                )
            if service == "radarr":
                self._radarr_posters = cache
            else:
                self._sonarr_posters = cache
        return cache.get(title, "")


def _sync_recent_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    active_ids: set[str],
    active_titles: set[str],
    poster_lookup: _PosterLookup,
) -> RecentDownloadItem | None:
    """Process one ``recent_downloads`` row; return its item, or ``None`` if dropped.

    Per-row side effects (preserved verbatim from the original loop body):

    * If the item has reappeared in the active queue, ``DELETE`` the row,
      ``commit()``, and return ``None`` (the caller skips it).
    * Otherwise, when the stored poster is missing, look it up via
      *poster_lookup* and, on a hit, ``UPDATE`` the row and ``commit()``.
    """
    if row["dl_id"] in active_ids or row["title"] in active_titles:
        # Item is back in the download queue — remove from recent
        conn.execute("DELETE FROM recent_downloads WHERE id = ?", (row["id"],))
        conn.commit()
        return None

    poster_url = row["poster_url"] or ""
    if not poster_url:
        dl_id = row["dl_id"] or ""
        service = (
            "radarr"
            if dl_id.startswith("radarr:")
            else ("sonarr" if dl_id.startswith("sonarr:") else "")
        )
        if not service:
            service = "radarr" if row["media_type"] == "movie" else "sonarr"
        poster_url = poster_lookup.poster_for(service, row["title"])
        if poster_url:
            try:
                conn.execute(
                    "UPDATE recent_downloads SET poster_url = ? WHERE id = ?",
                    (poster_url, row["id"]),
                )
                conn.commit()
            except sqlite3.Error:
                logger.warning(
                    "Failed to backfill poster for %s",
                    row["title"],
                    exc_info=True,
                )

    return {
        "id": row["dl_id"],
        "title": row["title"],
        "media_type": row["media_type"],
        "poster_url": poster_url,
        "completed_at": row["completed_at"],
    }


def fetch_and_sync_recent_downloads(
    conn: sqlite3.Connection,
    active_ids: set[str],
    active_titles: set[str],
    secret_key: str,
) -> list[RecentDownloadItem]:
    """Return recent downloads (last 7 days), excluding anything active.

    Side effects: deletes rows whose item has reappeared in the active queue
    (e.g. Radarr re-grabbed after a bad release) and backfills missing poster
    URLs via Radarr/Sonarr. The per-row work — including the DELETE/UPDATE
    and their commits — lives in :func:`_sync_recent_row`.
    """
    recent_rows = conn.execute(
        "SELECT id, dl_id, title, media_type, poster_url, completed_at"
        " FROM recent_downloads"
        " WHERE completed_at >= datetime('now', '-7 days')"
        " ORDER BY completed_at DESC"
    ).fetchall()
    poster_lookup = _PosterLookup(conn, secret_key)

    recent: list[RecentDownloadItem] = []
    for r in recent_rows:
        item = _sync_recent_row(conn, r, active_ids, active_titles, poster_lookup)
        if item is not None:
            recent.append(item)
    return recent
