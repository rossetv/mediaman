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
from collections.abc import Mapping
from typing import NotRequired, TypedDict, cast

import requests

from mediaman.services.arr._types import RadarrMovie, SonarrSeries
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.fetcher import ArrCard
from mediaman.services.arr.state import series_has_files
from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)


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


class _ArrLibraryIndex:
    """Lazy-loaded, call-scoped index of Radarr/Sonarr libraries.

    Fetches each service's library at most once per :func:`record_verified_completions`
    call and indexes by tmdbId + title for O(1) lookups.  ``None`` signals
    "not yet fetched"; an empty dict means "fetched, nothing found".

    Use :meth:`radarr_by_id` / :meth:`radarr_by_title` / :meth:`sonarr_by_id` /
    :meth:`sonarr_by_title` to access indexes — each calls the appropriate
    ``ensure_*`` method and returns a non-optional dict.
    """

    def __init__(self, conn: sqlite3.Connection, secret_key: str) -> None:
        self._conn = conn
        self._secret_key = secret_key
        self._radarr_by_id: dict[int, RadarrMovie] | None = None
        self._radarr_by_title: dict[str, RadarrMovie] | None = None
        self._sonarr_by_id: dict[int, SonarrSeries] | None = None
        self._sonarr_by_title: dict[str, SonarrSeries] | None = None

    def ensure_radarr(self) -> None:
        if self._radarr_by_id is not None:
            return
        from mediaman.services.arr.build import build_radarr_from_db

        # Build indexes before marking as fetched — if get_movies() raises,
        # _radarr_by_id stays None so the next item retries (matching original behaviour).
        by_id: dict[int, RadarrMovie] = {}
        by_title: dict[str, RadarrMovie] = {}
        client = build_radarr_from_db(self._conn, self._secret_key)
        for m in client.get_movies() if client else []:
            if tid := m.get("tmdbId"):
                by_id[int(tid)] = m
            if t := (m.get("title") or ""):
                by_title[t] = m
        self._radarr_by_id, self._radarr_by_title = by_id, by_title

    def ensure_sonarr(self) -> None:
        if self._sonarr_by_id is not None:
            return
        from mediaman.services.arr.build import build_sonarr_from_db

        by_id: dict[int, SonarrSeries] = {}
        by_title: dict[str, SonarrSeries] = {}
        client = build_sonarr_from_db(self._conn, self._secret_key)
        for s in client.get_series() if client else []:
            if tid := s.get("tmdbId"):
                by_id[int(tid)] = s
            if t := (s.get("title") or ""):
                by_title[t] = s
        self._sonarr_by_id, self._sonarr_by_title = by_id, by_title

    def radarr_index_by_id(self) -> dict[int, RadarrMovie]:
        """Return the Radarr-by-tmdbId index, fetching it if not yet loaded."""
        self.ensure_radarr()
        assert self._radarr_by_id is not None  # ensure_radarr post-condition
        return self._radarr_by_id

    def radarr_index_by_title(self) -> dict[str, RadarrMovie]:
        """Return the Radarr-by-title index, fetching it if not yet loaded."""
        self.ensure_radarr()
        assert self._radarr_by_title is not None  # ensure_radarr post-condition
        return self._radarr_by_title

    def sonarr_index_by_id(self) -> dict[int, SonarrSeries]:
        """Return the Sonarr-by-tmdbId index, fetching it if not yet loaded."""
        self.ensure_sonarr()
        assert self._sonarr_by_id is not None  # ensure_sonarr post-condition
        return self._sonarr_by_id

    def sonarr_index_by_title(self) -> dict[str, SonarrSeries]:
        """Return the Sonarr-by-title index, fetching it if not yet loaded."""
        self.ensure_sonarr()
        assert self._sonarr_by_title is not None  # ensure_sonarr post-condition
        return self._sonarr_by_title


def _check_item_verified(c: CompletedItem, idx: _ArrLibraryIndex) -> bool:
    """Return True if the completed item is confirmed by Radarr/Sonarr (or is NZB-only).

    Raises on fetch errors so the caller can log and skip the item.
    """
    dl_id = c["dl_id"]
    title = c["title"]
    if dl_id.startswith("radarr:"):
        tmdb_id = c.get("tmdb_id")
        movie = idx.radarr_index_by_id().get(int(tmdb_id)) if tmdb_id else None
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
            movie = idx.radarr_index_by_title().get(title)
        return movie is not None and bool(movie.get("hasFile"))
    if dl_id.startswith("sonarr:"):
        tmdb_id = c.get("tmdb_id")
        series = idx.sonarr_index_by_id().get(int(tmdb_id)) if tmdb_id else None
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
            series = idx.sonarr_index_by_title().get(title)
        return series is not None and series_has_files(series)
    # NZBGet-only items — no Arr verification possible
    return True


def _batch_insert_completions(
    conn: sqlite3.Connection, to_insert: list[tuple[str, str, str, str]]
) -> None:
    """Insert verified completion rows in a single batch commit."""
    if not to_insert:
        return
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO recent_downloads "
            "(dl_id, title, media_type, poster_url) VALUES (?, ?, ?, ?)",
            to_insert,
        )
        conn.commit()
    except sqlite3.Error:
        logger.warning(
            "Failed to record %d completed download(s)",
            len(to_insert),
            exc_info=True,
        )


def record_verified_completions(
    conn: sqlite3.Connection,
    completed: list[CompletedItem],
    secret_key: str,
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
    propagates ``tmdb_id`` from the queue snapshot, the fallback
    becomes vanishingly rare.
    """
    idx = _ArrLibraryIndex(conn, secret_key)
    to_insert: list[tuple[str, str, str, str]] = []

    for c in completed:
        try:
            verified = _check_item_verified(c, idx)
        except (SafeHTTPError, requests.RequestException, ArrError):
            logger.warning(
                "Failed to verify completion for %s — skipping", c["dl_id"], exc_info=True
            )
            continue

        if not verified:
            logger.info("Skipping completion for %s — no files confirmed", c["dl_id"])
            continue

        to_insert.append((c["dl_id"], c["title"], c["media_type"], c["poster_url"]))

    # Single batch insert + single commit — per-row commits fsync once each,
    # which dominates the loop cost on a large completion set.
    _batch_insert_completions(conn, to_insert)


def fetch_and_sync_recent_downloads(
    conn: sqlite3.Connection,
    active_ids: set[str],
    active_titles: set[str],
    secret_key: str,
) -> list[RecentDownloadItem]:
    """Return recent downloads (last 7 days), excluding anything active.

    Side effects: deletes rows whose item has reappeared in the active queue
    (e.g. Radarr re-grabbed after a bad release) and backfills missing poster
    URLs via Radarr/Sonarr.
    """
    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db

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
                client = (
                    build_radarr_from_db(conn, secret_key)
                    if service == "radarr"
                    else build_sonarr_from_db(conn, secret_key)
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
                except sqlite3.Error:
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
