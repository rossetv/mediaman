"""Arr-library verification helpers for completed download records.

Owns the call-scoped index (:class:`_ArrLibraryIndex`), the per-item
verification predicate (:func:`_check_item_verified`), the batch DB
insert (:func:`_batch_insert_completions`), and the public entry point
(:func:`record_verified_completions`).

Kept separate from the sync/persistence helpers so verification logic
(which depends on Radarr/Sonarr HTTP) is independently testable and
replaceable without touching the queue-sync path.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

import requests

from mediaman.services.arr._types import RadarrMovie, SonarrSeries
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.state import series_has_files
from mediaman.services.infra import SafeHTTPError

if TYPE_CHECKING:
    pass

from mediaman.services.arr.completion._types import CompletedItem

logger = logging.getLogger(__name__)


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
