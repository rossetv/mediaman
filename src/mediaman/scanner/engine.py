"""Scan engine — orchestrates a full Plex library scan.

For each configured library the engine:

1. Fetches all items from Plex (via :mod:`phases.fetch`).
2. Upserts each item into ``media_items`` (via :mod:`phases.upsert`).
3. Skips items that are protected (forever or active snooze).
4. Skips items already awaiting deletion.
5. Evaluates eligibility via :mod:`phases.evaluate`.
6. Schedules eligible items: inserts into ``scheduled_actions`` with an
   HMAC token and marks re-entries where a prior snooze has expired.
7. Writes an ``audit_log`` entry for every scheduled action.
8. Sends a newsletter to all active subscribers via Mailgun.

The SQL, Plex I/O, Arr-date caching, and deletion execution each live
in their own module; this module is orchestration only.

Import-cycle rule: :mod:`repository` imports nothing from
:mod:`fetch` / :mod:`deletions`; :mod:`deletions` may import
:mod:`repository`; :mod:`fetch` may import :mod:`repository`; this
module orchestrates all three.
"""

# rationale: scan orchestrator — owns the per-library commit boundary so a
# SIGKILL mid-scan can only roll back the in-flight library, never a successful
# upsert from an earlier one. The per-library scan body lives in
# :mod:`_scan_library` so this shell stays scannable; the helpers there do not
# commit, leaving the transaction boundary visible in :meth:`run_scan`.

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING

import requests
from plexapi.exceptions import PlexApiException

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient
    from mediaman.services.media_meta.plex import PlexClient

from mediaman.core.format import ensure_tz as _ensure_tz
from mediaman.core.time import parse_iso_utc as _parse_iso_utc
from mediaman.scanner import repository
from mediaman.scanner._scan_library import scan_movie_library, scan_tv_library
from mediaman.scanner.arr_dates import ArrDateCache
from mediaman.scanner.deletions import (
    DeletionExecutor,
    DeletionResult,
)
from mediaman.scanner.fetch import PlexFetcher, _PlexItemFetch
from mediaman.scanner.phases.delete import remove_orphans
from mediaman.scanner.phases.upsert import upsert_item as _phase_upsert_item
from mediaman.services.infra import get_bool_setting as _get_bool_setting
from mediaman.services.mail.newsletter import send_newsletter as _send_newsletter
from mediaman.services.openai.recommendations.persist import (
    refresh_recommendations as _refresh_recommendations,
)

logger = logging.getLogger(__name__)

__all__ = ["ScanEngine"]


def _coerce_lib_ids(raw: Iterable[str]) -> set[int]:
    """Coerce an iterable of library ID strings to a set of ints.

    Raises:
        ValueError: If any entry cannot be coerced to an integer.  A
            malformed library ID (e.g. ``"all"``, an empty string) is a
            configuration error that would produce a silently-incomplete
            scan; surfacing it as an exception at the boundary is
            preferable to a warning that may go unnoticed.
    """
    result: set[int] = set()
    for lib_id in raw:
        try:
            result.add(int(lib_id))
        except (ValueError, TypeError):
            raise ValueError(f"malformed library id: {lib_id!r}") from None
    return result


class ScanEngine:
    """Orchestrates a full library scan across one or more Plex library sections.

    Args:
        conn: Open SQLite connection (with row_factory set to sqlite3.Row).
        plex_client: Object providing ``get_movie_items``, ``get_show_seasons``,
            ``get_watch_history``, and ``get_season_watch_history``.
        library_ids: Ordered list of Plex section IDs to scan.
        library_types: Mapping of library_id -> ``"movie"`` or ``"show"``.
        library_titles: Mapping of library_id -> lowercase library title
            (e.g. ``"anime"``).
        secret_key: HMAC secret used to sign keep tokens.
        min_age_days: Minimum days since added before eligibility is assessed.
        inactivity_days: Days without a watch event before deletion is triggered.
        grace_days: Days from *now* until the scheduled deletion executes.
        dry_run: When True, the scan continues to upsert media-item rows so
            the library catalogue stays current, but **no deletion-state
            changes are written**: ``schedule_deletion`` is skipped, orphan
            removal is skipped, the newsletter is not sent, recommendations
            are not refreshed, and the on-disk rm + ``cleanup_expired_snoozes``
            in the deletion executor are also skipped. Use this for
            "what would happen" previews. Defaults to False.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        plex_client: PlexClient,
        library_ids: list[str],
        library_types: dict[str, str],
        library_titles: dict[str, str] | None = None,
        secret_key: str,
        min_age_days: int = 30,
        inactivity_days: int = 30,
        grace_days: int = 14,
        dry_run: bool = False,
        sonarr_client: ArrClient | None = None,
        radarr_client: ArrClient | None = None,
    ) -> None:
        self._conn = conn
        self._plex = plex_client
        self._library_ids = library_ids
        self._library_types = library_types
        self._library_titles = library_titles or {}
        self._secret_key = secret_key
        self._min_age_days = min_age_days
        self._inactivity_days = inactivity_days
        self._grace_days = grace_days
        self._dry_run = dry_run
        self._sonarr = sonarr_client
        self._radarr = radarr_client

        self._fetcher = PlexFetcher(
            plex_client=plex_client,
            library_types=library_types,
            library_titles=self._library_titles,
        )
        self._arr_cache = ArrDateCache(
            radarr_client=radarr_client,
            sonarr_client=sonarr_client,
        )
        self._deletions = DeletionExecutor(
            conn=conn,
            dry_run=dry_run,
            cleanup_snoozes=not dry_run,
            sonarr_client=sonarr_client,
            radarr_client=radarr_client,
        )

    def _load_delete_allowed_roots(self) -> list[str]:
        return repository.read_delete_allowed_roots_setting(self._conn)

    def _ensure_arr_dates(self) -> None:
        self._arr_cache.ensure_loaded()

    @property
    def _arr_dates(self) -> dict[str, str]:
        return self._arr_cache.dates()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_library(self) -> dict[str, int]:
        """Sync media items from Plex without evaluating for deletion.

        A lightweight alternative to :meth:`run_scan` that only fetches
        current library state from Plex and updates the ``media_items``
        table. No eligibility checks, no deletions, no newsletter.

        Also removes orphaned entries whose ``plex_rating_key`` no longer
        exists in Plex (e.g. after a delete-and-redownload cycle where
        Plex assigns a new key).

        Split into two phases so network I/O never overlaps with an open
        SQLite write transaction:

        1. **Fetch phase** — pull every library's items and their watch
           history from Plex into an in-memory buffer. No DB writes.
        2. **Write phase** — UPSERTs with one ``commit()`` per library
           so the SQLite write lock is held only for a single library's
           worth of writes at a time.
        """
        summary = {"synced": 0, "errors": 0, "removed": 0}
        per_lib_fetches = self._sync_phase_fetch(summary)
        self._sync_phase_write(per_lib_fetches, summary)
        logger.info(
            "Library sync complete: %d synced, %d orphans removed, %d errors",
            summary["synced"],
            summary["removed"],
            summary["errors"],
        )
        return summary

    def _sync_phase_fetch(self, summary: dict[str, int]) -> dict[str, list[_PlexItemFetch]]:
        """Pull every library's items + watch history from Plex into memory.

        Returns a ``lib_id -> [PlexItemFetch]`` mapping. No DB writes
        happen here. Fetch failures are logged per-library and
        accumulate into ``summary["errors"]`` so a single bad library
        cannot abort the whole sync. The mapping only contains
        successfully-fetched libraries so the write phase will not
        observe a partial fetch.
        """
        per_lib_fetches: dict[str, list[_PlexItemFetch]] = {}
        for lib_id in self._library_ids:
            try:
                per_lib_fetches[lib_id] = self._fetcher.fetch_library_items(lib_id)
            except (PlexApiException, requests.RequestException, sqlite3.Error):
                # skip-one-bad-library — a fetch failure must not abort other libraries
                logger.exception("Library sync failed for library %s", lib_id)
                summary["errors"] += 1
        return per_lib_fetches

    def _sync_phase_write(
        self,
        per_lib_fetches: dict[str, list[_PlexItemFetch]],
        summary: dict[str, int],
    ) -> None:
        """Apply per-library UPSERTs + orphan cleanup, one transaction each.

        No network calls happen here, so the SQLite write lock is held
        only for one library's UPSERTs at a time. A malformed ``lib_id``
        (rare — would mean the Plex section tree contains a non-numeric
        key, which the API does not currently emit but could on a
        corrupted server) skips orphan removal for that library rather
        than crashing the whole sync.
        """
        for lib_id, fetches in per_lib_fetches.items():
            seen_lib_keys: set[str] = {f.item["plex_rating_key"] for f in fetches}
            for f in fetches:
                _phase_upsert_item(self._conn, f, self._arr_cache, f.media_type)
                summary["synced"] += 1
            try:
                lib_set = _coerce_lib_ids([lib_id])
            except ValueError:
                logger.warning(
                    "scanner.lib_sync.malformed_lib_id lib_id=%r — skipping "
                    "orphan removal for this library",
                    lib_id,
                )
                self._conn.commit()
                continue
            summary["removed"] += self._remove_orphaned_items(seen_lib_keys, lib_set)
            self._conn.commit()

    def run_scan(self) -> dict[str, int]:
        """Execute a full scan and return a summary dict.

        Returns:
            Dict with the following integer keys:

            - ``scanned``: total items examined across all libraries.
            - ``scheduled``: items newly scheduled for deletion this run.
            - ``skipped``: items skipped (protected, already scheduled, or ineligible).
            - ``errors``: items that raised an unexpected exception during processing.
            - ``removed``: orphaned DB rows whose Plex rating key no longer exists.
            - ``deleted``: items whose grace period elapsed and were deleted from disk.
            - ``reclaimed_bytes``: total bytes freed by deletions this run.
        """
        summary = {
            "scanned": 0,
            "scheduled": 0,
            "skipped": 0,
            "errors": 0,
            "removed": 0,
        }
        seen_by_lib = self._scan_all_libraries(summary)
        self._cleanup_orphans_per_library(seen_by_lib, summary)
        self._record_deletion_outcome(summary)
        self._run_post_scan_followups()
        return summary

    def _scan_all_libraries(self, summary: dict[str, int]) -> dict[str, set[str]]:
        """Run the per-library scan and return the ``lib_id -> seen-keys`` map.

        Each library commits separately (per-library commit boundary) so
        a SIGKILL mid-scan can only roll back the in-flight library, not
        every successful upsert from earlier libraries. Libraries with
        unknown or unsupported types are logged and skipped — an
        unmapped library would silently default to ``"movie"``, so a
        Plex library that has been re-typed (e.g. moved to ``"music"``
        or ``"photo"``) would be scanned as if it were a movie
        collection.
        """
        seen_by_lib: dict[str, set[str]] = {}
        for lib_id in self._library_ids:
            lib_type = self._library_types.get(lib_id)
            if lib_type is None:
                logger.warning(
                    "engine.run_scan.unknown_library_type lib_id=%s — skipping; "
                    "Plex library type is unknown to the scanner. "
                    "Re-fetch library metadata or remove the library from settings.",
                    lib_id,
                )
                continue
            if lib_type not in {"movie", "show"}:
                logger.warning(
                    "engine.run_scan.unsupported_library_type lib_id=%s type=%s — "
                    "skipping; the scanner only supports 'movie' and 'show' libraries.",
                    lib_id,
                    lib_type,
                )
                continue

            seen: set[str] = set()
            seen_by_lib[lib_id] = seen
            if lib_type == "show":
                scan_tv_library(self, lib_id, summary, seen)
            else:
                scan_movie_library(self, lib_id, summary, seen)
            self._conn.commit()
        return seen_by_lib

    def _cleanup_orphans_per_library(
        self, seen_by_lib: dict[str, set[str]], summary: dict[str, int]
    ) -> None:
        """Remove orphan ``media_items`` rows for each library independently.

        Each library is evaluated alone so a single library's empty
        result cannot wipe items belonging to another library. Skipped
        entirely in dry-run mode.
        """
        if self._dry_run:
            logger.info("engine.run_scan.dry_run skipping orphan removal")
            return
        for lib_id, seen in seen_by_lib.items():
            try:
                lib_set = _coerce_lib_ids([lib_id])
            except ValueError:
                logger.warning(
                    "scanner.run_scan.malformed_lib_id lib_id=%r — "
                    "skipping orphan removal for this library",
                    lib_id,
                )
                continue
            summary["removed"] += self._remove_orphaned_items(seen, lib_set)
        self._conn.commit()

    def _record_deletion_outcome(self, summary: dict[str, int]) -> None:
        """Execute pending deletions and record their counts in *summary*."""
        deletion_result = self.execute_deletions()
        summary["deleted"] = deletion_result["deleted"]
        summary["reclaimed_bytes"] = deletion_result["reclaimed_bytes"]

    def _run_post_scan_followups(self) -> None:
        """Refresh AI recommendations and send the deletion-warning newsletter.

        Recommendations refresh runs FIRST so the newsletter reflects
        this week's picks rather than last week's stale batch — the
        cards are loaded from the ``suggestions`` table that
        :func:`_refresh_recommendations` rewrites. Both calls are
        wrapped in narrow exception handlers because either failing
        must not abort a successful scan. Skipped entirely in dry-run
        mode.
        """
        if self._dry_run:
            logger.info("engine.run_scan.dry_run skipping newsletter + recommendations refresh")
            return
        if _get_bool_setting(self._conn, "suggestions_enabled", default=True):
            try:
                _refresh_recommendations(self._conn, self._plex, secret_key=self._secret_key)
            except (PlexApiException, requests.RequestException, sqlite3.Error):
                # best-effort post-scan followup — must not abort scan summary
                logger.exception("Recommendation generation failed — scan results unaffected")
        try:
            _send_newsletter(
                conn=self._conn,
                secret_key=self._secret_key,
                dry_run=self._dry_run,
                grace_days=self._grace_days,
            )
        except (PlexApiException, requests.RequestException, sqlite3.Error):
            # best-effort post-scan followup — must not abort scan summary
            logger.exception("Newsletter sending failed — scan results unaffected")

    def execute_deletions(self) -> DeletionResult:
        """Execute pending deletions where the grace period has passed.

        Delegates to :class:`DeletionExecutor`. Kept on ``ScanEngine``
        so existing callers don't need to know about the split.
        """
        return self._deletions.execute()

    # ------------------------------------------------------------------
    # Internal helpers — scan orchestration
    # ------------------------------------------------------------------

    def _resolve_added_at(self, item: dict[str, object]) -> datetime:
        """Return the best available 'added' datetime for a media item.

        Prefers the Arr download date (looked up by normalised file
        path) because it reflects when the file actually landed on disk,
        which is more accurate than the Plex ``added_at`` date. Falls
        back to Plex's ``added_at`` when no Arr record exists. Plex's
        ``updated_at`` is used only as a last resort because it tracks
        the **last metadata refresh** — every subtitle download or
        poster refresh resets it, which would mask deletion eligibility
        indefinitely. Falls back to ``datetime.now(UTC)`` via
        ``ensure_tz(None)`` when nothing usable is present.
        """
        file_path = item.get("file_path") or ""
        if not isinstance(file_path, str):
            file_path = ""
        arr_date_str = self._arr_cache.get(file_path)
        if arr_date_str:
            parsed = _parse_iso_utc(arr_date_str)
            if parsed is not None:
                return _ensure_tz(parsed)
            # An Arr cache hit with an unparseable date used to be
            # silently substituted with ``datetime.now(UTC)`` — that
            # made every affected item look freshly added and gave it
            # permanent protection from deletion. Log the bad value
            # and fall through to the Plex ``added_at`` chain so the
            # item is evaluated normally.
            logger.warning(
                "engine.resolve_added_at.bad_arr_date file_path=%r value=%r — "
                "falling through to Plex added_at",
                item.get("file_path", ""),
                arr_date_str,
            )
        # Plex ``added_at`` is the file-arrival time and is the
        # correct source. ``updated_at`` is a metadata-refresh marker;
        # using it would mean every subtitle/poster refresh resets
        # the eligibility clock, so it must only be a last-resort
        # fallback when ``added_at`` is missing entirely.
        candidate = item.get("added_at") or item.get("updated_at")
        if candidate is not None and not isinstance(candidate, datetime):
            candidate = None
        return _ensure_tz(candidate)

    def _remove_orphaned_items(
        self,
        seen_keys: set[str],
        scanned_libs: set[int],
    ) -> int:
        """Remove ``media_items`` whose ``plex_rating_key`` is gone from Plex.

        Delegates to :func:`phases.delete.remove_orphans` which owns the
        fail-closed safeguard logic (C31). Only considers items belonging
        to libraries that were successfully scanned.
        """
        return remove_orphans(self._conn, seen_keys, scanned_libs)
