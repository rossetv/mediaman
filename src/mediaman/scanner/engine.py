"""Scan engine — orchestrates a full Plex library scan.

For each configured library the engine:

1. Fetches all items from Plex (via phases.fetch).
2. Upserts each item into ``media_items`` (via phases.upsert).
3. Skips items that are protected (forever or active snooze).
4. Skips items already awaiting deletion.
5. Evaluates eligibility via phases.evaluate.
6. Schedules eligible items: inserts into ``scheduled_actions`` with an
   HMAC token and marks re-entries where a prior snooze has expired.
7. Writes an ``audit_log`` entry for every scheduled action.
8. Sends a newsletter to all active subscribers via Mailgun.

The SQL, Plex I/O, Arr-date caching, and deletion execution each live
in their own module; this module is orchestration only.

Import-cycle rule: repository imports nothing from
fetch / deletions; deletions may import
repository; fetch may import repository; this
module orchestrates all three.
"""

# rationale: scan orchestrator — owns the per-library commit boundary so a
# SIGKILL mid-scan can only roll back the in-flight library, never a successful
# upsert from an earlier one. The per-library scan body lives in
# _scan_library so this shell stays scannable; the helpers there do not
# commit, leaving the transaction boundary visible in run_scan.

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from xml.etree.ElementTree import ParseError

import requests
from plexapi.exceptions import PlexApiException

if TYPE_CHECKING:
    from mediaman.scanner.runner import ScanSummary
    from mediaman.services.arr.base import ArrClient
    from mediaman.services.media_meta.plex import PlexClient

from mediaman.core.format import ensure_tz as _ensure_tz
from mediaman.core.time import now_iso, now_utc
from mediaman.core.time import parse_iso_utc as _parse_iso_utc
from mediaman.scanner import repository
from mediaman.scanner._scan_library import (
    begin_write,
    item_savepoint,
    scan_movie_library,
    scan_tv_library,
    with_mediaman_play,
)
from mediaman.scanner.arr_dates import ArrDateCache
from mediaman.scanner.deletions import (
    DeletionExecutor,
    DeletionResult,
)
from mediaman.scanner.fetch import FetchedLibrary, PlexFetcher, PlexItemFetch
from mediaman.scanner.phases.delete import remove_orphans
from mediaman.scanner.phases.upsert import upsert_item as _phase_upsert_item
from mediaman.scanner.repository.scheduled_actions import fetch_gone_library_ids
from mediaman.services import watch_catalog
from mediaman.services.infra import SafeHTTPError
from mediaman.services.infra import get_bool_setting as _get_bool_setting
from mediaman.services.mail.newsletter import NewsletterConfigError
from mediaman.services.mail.newsletter import send_newsletter as _send_newsletter
from mediaman.services.openai.recommendations.persist import (
    prune_old_picks as _prune_old_picks,
)
from mediaman.services.openai.recommendations.persist import (
    refresh_recommendations as _refresh_recommendations,
)
from mediaman.services.openai.recommendations.personal import (
    refresh_people as _refresh_people,
)
from mediaman.services.plex_link.sync import read_plays as _read_plays

logger = logging.getLogger(__name__)

# What reading a library from Plex may raise.
_FETCH_ERRORS = (
    PlexApiException,
    requests.RequestException,
    SafeHTTPError,
    ParseError,
    ValueError,
)

__all__ = ["ScanEngine"]

# Settings key holding when each library with items that Plex no longer lists was first found gone.
_GONE_LIBRARIES_KEY = "gone_libraries"
# How long after it was first found gone a run must begin to confirm it: every run in a short
# spell may ask a Plex that has just started and does not list all its libraries yet.
_GONE_FOR = timedelta(hours=1)
# Off: what it would do to the file of a kept copy is not yet certain.
_PRUNE_GONE_LIBRARIES = False


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
        library_types: Mapping of library_id -> its type, for every library Plex
            lists, scanned or not. A library it lacks is gone from Plex, unless
            it is empty.
        library_titles: Mapping of library_id -> library title (e.g. ``"Anime"``).
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
        self._library_titles = {
            lib_id: title.lower() for lib_id, title in (library_titles or {}).items()
        }
        self._secret_key = secret_key
        self._min_age_days = min_age_days
        self._inactivity_days = inactivity_days
        self._grace_days = grace_days
        self._dry_run = dry_run
        self._sonarr = sonarr_client
        self._radarr = radarr_client
        # When the current run began, before it listed anything in Plex.
        self._started_at: str | None = None
        # The keys Plex lists in the current run, as far as it has been read.
        self._listed_keys: set[str] = set()
        # The path and season of each key whose history the current scan could not fetch.
        self._skipped_places: set[tuple[object, object]] = set()
        # Why each library the current scan could not read from Plex was left out of it.
        self._left_out: dict[str, str] = {}

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
            inactivity_days=inactivity_days,
            sonarr_client=sonarr_client,
            radarr_client=radarr_client,
        )

    def sync_library(self) -> dict[str, int]:
        """Sync media items from Plex without evaluating for deletion.

        A lightweight alternative to run_scan that only fetches
        current library state from Plex and updates the ``media_items``
        table. No eligibility checks, no deletions, no newsletter.

        Also removes orphaned entries whose ``plex_rating_key`` no longer
        exists in Plex (e.g. after a delete-and-redownload cycle where
        Plex assigns a new key).

        Split into two phases so network I/O never overlaps with an open
        SQLite write transaction:

        1. **Fetch phase** — pull every library's items and their watch
           history from Plex into an in-memory buffer, then load the
           Radarr/Sonarr dates and index. No DB writes.
        2. **Write phase** — UPSERTs with one ``commit()`` per library
           so the SQLite write lock is held only for a single library's
           worth of writes at a time.
        """
        self._started_at = now_iso()
        self._listed_keys = set()
        summary = {"synced": 0, "errors": 0, "removed": 0}
        try:
            per_lib_fetches = self._sync_phase_fetch(summary)
            self._arr_cache.ensure_loaded()
            self._sync_phase_write(per_lib_fetches, summary)
        except BaseException:
            self._roll_back_failed_run()
            raise
        finally:
            # Libraries committed before a failure are already in the DB.
            watch_catalog.clear_library_index()
        logger.info(
            "Library sync complete: %d synced, %d orphans removed, %d errors",
            summary["synced"],
            summary["removed"],
            summary["errors"],
        )
        return summary

    def _roll_back_failed_run(self) -> None:
        """Drop the half-written library a failed sync phase leaves open on the connection.

        Callers that do not roll back (the manual scan, the sync route) would
        otherwise commit it with their next write. Each library is committed
        as it goes, so only the in-flight one is lost. A failing rollback is
        logged: it must not hide the error being raised.
        """
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except sqlite3.Error:
            logger.exception("Rollback after a failed scan or library sync failed")

    def _sync_phase_fetch(self, summary: dict[str, int]) -> dict[str, FetchedLibrary]:
        """Pull every library's items + watch history from Plex into memory.

        One of unknown or unsupported type is left out: what it holds would be
        stored as films, and what it held pruned.

        Returns a ``lib_id -> FetchedLibrary`` mapping. No DB writes
        happen here. Fetch failures are logged per-library and
        accumulate into ``summary["errors"]`` so a single bad library
        cannot abort the whole sync. The mapping only contains
        successfully-fetched libraries so the write phase will not
        observe a partial fetch.
        """
        per_lib_fetches: dict[str, FetchedLibrary] = {}
        for lib_id in self._library_ids:
            if self._unreadable(lib_id) is not None:
                summary["errors"] += 1
                continue
            try:
                per_lib_fetches[lib_id] = self._fetcher.fetch_library_items(lib_id)
            except (*_FETCH_ERRORS, sqlite3.Error):
                # skip-one-bad-library — a fetch failure must not abort other libraries
                logger.exception("Library sync failed for library %s", lib_id)
                summary["errors"] += 1
        return per_lib_fetches

    def _sync_phase_write(
        self,
        per_lib_fetches: dict[str, FetchedLibrary],
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
        # Read once per run: the sync's fetch phase is over and plays made
        # while it writes are picked up by the next sync.
        credited = repository.last_credited_by_media_item(
            self._conn,
            [
                f.item["plex_rating_key"]
                for fetched in per_lib_fetches.values()
                for f in fetched.items
            ],
        )
        begin_write(self._conn)
        repository.carry_stranded_keeps(self._conn)
        for fetched in per_lib_fetches.values():
            self._listed_keys.update(fetched.listed_keys)
        for lib_id, fetched in per_lib_fetches.items():
            begin_write(self._conn)
            repository.carry_show_keeps(self._conn, fetched.seasons, self._listed_keys)
            # Includes the history-fetch skips: those items are still present in
            # Plex, just not fetched this pass, so they must never be pruned as orphans.
            seen_lib_keys = fetched.listed_keys
            for f in fetched.items:
                if not self._upsert_item_isolated(with_mediaman_play(f, credited)):
                    summary["errors"] += 1
                    continue
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
        summary["removed"] += self._remove_items_of_gone_libraries()
        repository.restore_progress(self._conn)
        # A sync's engine is always a dry run, so the person's own setting decides.
        self._settle_keeps(dry_run=repository.read_setting(self._conn, "dry_run") == "true")

    def _settle_keeps(self, *, dry_run: bool) -> None:
        """Let every key of a kept title have what its keep means, now that all are stored."""
        begin_write(self._conn)
        repository.carry_film_keeps(self._conn)
        if not dry_run:
            repository.cancel_deletions_kept_under_another_key(self._conn)
        self._conn.commit()

    def _upsert_item_isolated(self, f: PlexItemFetch) -> bool:
        """Upsert one item inside its own savepoint; return False if it failed.

        A failure rolls back only this item so the rest of the library
        still syncs.
        """
        try:
            with item_savepoint(self._conn):
                _phase_upsert_item(self._conn, f, self._arr_cache, f.media_type, self._listed_keys)
        except (KeyError, TypeError, ValueError, RuntimeError, sqlite3.Error):
            # rationale: per-item isolation, same narrow set as the scan loop
            logger.exception(
                "Library sync item failed (plex_rating_key=%s)",
                f.item.get("plex_rating_key", "?"),
            )
            return False
        return True

    def run_scan(self) -> ScanSummary:
        """Execute a full scan and return a summary dict.

        Returns:
            Dict with the following integer keys:

            - ``scanned``: total items examined across all libraries.
            - ``scheduled``: items newly scheduled for deletion this run.
            - ``skipped``: items skipped (protected, already scheduled, or ineligible).
            - ``errors``: items that raised an unexpected exception during processing,
              and libraries Plex failed to answer for.
            - ``removed``: orphaned DB rows whose Plex rating key no longer exists.
            - ``deleted``: items whose grace period elapsed and were deleted from disk.
            - ``reclaimed_bytes``: total bytes freed by deletions this run.
            - ``left_out``: why each library Plex was not read for was left out, by its id.
        """
        self._started_at = now_iso()
        self._listed_keys = set()
        self._skipped_places = set()
        self._left_out = {}
        summary = {
            "scanned": 0,
            "scheduled": 0,
            "skipped": 0,
            "errors": 0,
            "removed": 0,
        }
        try:
            try:
                # Radarr/Sonarr reads happen here, never inside a write transaction.
                self._arr_cache.ensure_loaded()
                seen_by_lib = self._scan_all_libraries(summary)
                self._cleanup_orphans_per_library(seen_by_lib, summary)
            except BaseException:
                self._roll_back_failed_run()
                raise
            # Not rolled back: a deletion may have removed files whose record is
            # still open, and recovery books it from that record.
            self._record_deletion_outcome(summary)
        finally:
            # Libraries committed before a failure are already in the DB.
            watch_catalog.clear_library_index()
        self._run_post_scan_followups()
        return {
            "scanned": summary["scanned"],
            "scheduled": summary["scheduled"],
            "skipped": summary["skipped"],
            "errors": summary["errors"],
            "removed": summary["removed"],
            "deleted": summary["deleted"],
            "reclaimed_bytes": summary["reclaimed_bytes"],
            "left_out": self._left_out,
        }

    def _scan_all_libraries(self, summary: dict[str, int]) -> dict[str, set[str]]:
        """Run the per-library scan and return the ``lib_id -> seen-keys`` map.

        Each library commits separately (per-library commit boundary) so
        a SIGKILL mid-scan can only roll back the in-flight library, not
        every successful upsert from earlier libraries. All are stored
        before any is evaluated, so an item is judged by the watches of
        its title in every library. One Plex does not list, that is of
        a type the scanner does not support, or that Plex fails to answer
        for is left out: nothing of it is pruned, scheduled or deleted in
        this run, nor anything scheduled that shares a place with it.
        """
        begin_write(self._conn)
        repository.carry_stranded_keeps(self._conn)
        self._conn.commit()
        seen_by_lib: dict[str, set[str]] = {}
        evaluations: list[Callable[[], None]] = []
        for lib_id in self._library_ids:
            if (reason := self._unreadable(lib_id)) is not None:
                self._leave_out(lib_id, reason, summary)
                continue

            lib_type = self._library_types[lib_id]
            seen: set[str] = set()
            scan_library = scan_tv_library if lib_type == "show" else scan_movie_library
            try:
                evaluate = scan_library(self, lib_id, summary, seen)
            except _FETCH_ERRORS:
                # skip-one-bad-library — a fetch failure must not abort other libraries
                logger.exception("Scan left out library %s: Plex failed to answer for it", lib_id)
                self._conn.rollback()
                self._leave_out(lib_id, "Plex failed to answer for it", summary)
                continue
            seen_by_lib[lib_id] = seen
            evaluations.append(evaluate)
            self._conn.commit()
        self._settle_keeps(dry_run=self._dry_run)
        for evaluate in evaluations:
            evaluate()
            self._conn.commit()
        return seen_by_lib

    def _unreadable(self, lib_id: str) -> str | None:
        """Return why library *lib_id* cannot be read whatever Plex answers, or None."""
        lib_type = self._library_types.get(lib_id)
        if lib_type is None:
            logger.warning(
                "engine.unknown_library_type lib_id=%s — left out; Plex does not list it. "
                "Untick it in Settings if it is gone.",
                lib_id,
            )
            return "Plex does not list it"
        if lib_type not in {"movie", "show"}:
            logger.warning(
                "engine.unsupported_library_type lib_id=%s type=%s — left out; only 'movie' "
                "and 'show' libraries are supported.",
                lib_id,
                lib_type,
            )
            return "Plex lists it as neither films nor shows"
        return None

    def _leave_out(self, lib_id: str, reason: str, summary: dict[str, int]) -> None:
        """Note that nobody knows what library *lib_id* holds now, nor who watched it."""
        summary["errors"] += 1
        self._left_out[lib_id] = reason
        if lib_id.isdecimal():
            ids = repository.fetch_ids_in_libraries(self._conn, [int(lib_id)])
            self._skipped_places.update(repository.fetch_places(self._conn, ids))

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
        summary["removed"] += self._remove_items_of_gone_libraries()
        repository.restore_progress(self._conn)
        self._conn.commit()

    def _record_deletion_outcome(self, summary: dict[str, int]) -> None:
        deletion_result = self.execute_deletions()
        summary["deleted"] = deletion_result["deleted"]
        summary["reclaimed_bytes"] = deletion_result["reclaimed_bytes"]

    def _run_post_scan_followups(self) -> None:
        """Read people's Plex plays, refresh AI recommendations and send the newsletter.

        Recommendations refresh runs FIRST so the newsletter reflects
        this week's picks rather than last week's stale batch — the
        cards are loaded from the ``suggestions`` and ``user_picks``
        tables that _refresh_recommendations and _refresh_people, which
        needs the plays _read_plays brings, rewrite. A failure of any step
        is logged and the next one still runs: none may abort a successful
        scan. Dry-run mode only reads the plays: that also drops those of
        libraries no longer read, whatever else is switched off.
        """
        try:
            _read_plays(self._conn, self._secret_key)
        except Exception:  # rationale: what Plex holds for a person must not cost a scan
            logger.exception("Reading Plex plays failed — scan results unaffected")
        if self._dry_run:
            logger.info("engine.run_scan.dry_run skipping newsletter + recommendations refresh")
            return
        try:
            _prune_old_picks(self._conn)
        except sqlite3.Error:
            logger.exception("Old picks were not removed — scan results unaffected")
        if _get_bool_setting(self._conn, "suggestions_enabled", default=True):
            try:
                _refresh_recommendations(self._conn, secret_key=self._secret_key)
            except Exception:  # rationale: what a model answers must never cost a scan
                logger.exception("Recommendation generation failed — scan results unaffected")
            try:
                _refresh_people(self._conn, self._plex, secret_key=self._secret_key)
            except Exception:  # rationale: what a model answers must never cost a scan
                logger.exception("Own picks failed — scan results unaffected")
        try:
            _send_newsletter(
                conn=self._conn,
                secret_key=self._secret_key,
                dry_run=self._dry_run,
            )
        except (
            PlexApiException,
            requests.RequestException,
            SafeHTTPError,
            sqlite3.Error,
            NewsletterConfigError,
            ValueError,
        ):
            # A half-configured Mailgun raises NewsletterConfigError or ValueError;
            # neither may fail a scan whose deletions already ran.
            logger.exception("Newsletter sending failed — scan results unaffected")

    def execute_deletions(self) -> DeletionResult:
        """Execute pending deletions where the grace period has passed.

        Delegates to DeletionExecutor. The items of a library this run could not
        read wait: one of them may no longer be what Plex lists. So does one that
        may be one of a library Plex does not list, or that has a file a keep or a
        late watch of a title removed from the library holds.
        """
        gone = (
            fetch_gone_library_ids(self._conn, self._library_types) if self._library_types else ()
        )
        return self._deletions.execute(unread_libraries=self._left_out, gone_libraries=gone)

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
            # Substituting ``datetime.now(UTC)`` for an unparseable Arr
            # date would make the item look freshly added and give it
            # permanent protection from deletion. Log the bad value
            # and fall through to the Plex ``added_at`` chain so the
            # item is evaluated normally.
            logger.warning(
                "engine.resolve_added_at.bad_arr_date file_path=%r value=%r — "
                "falling through to Plex added_at",
                item.get("file_path", ""),
                arr_date_str,
            )
        # Plex ``added_at`` is the file-arrival time and the only correct
        # eligibility source. ``updated_at`` is a metadata-refresh marker —
        # every subtitle/poster refresh resets it — so it is deliberately
        # NOT used as a fallback: doing so would (a) keep recently-touched
        # items permanently young and immune to deletion, and (b) diverge
        # from ``upsert_media_item``, which stores ``added_at or now`` and
        # never consults ``updated_at``. With ``updated_at`` dropped, the
        # eligibility clock and the persisted ``media_items.added_at`` agree.
        # A missing ``added_at`` falls straight to ``now`` via
        # ``_ensure_tz(None)`` — the item is treated as freshly added and
        # simply ages in on subsequent scans.
        candidate = item.get("added_at")
        if candidate is not None and not isinstance(candidate, datetime):
            candidate = None
        return _ensure_tz(candidate)

    def _remove_items_of_gone_libraries(self) -> int:
        """Record when each library Plex does not list was first found gone, and prune those.

        Pruned, while _PRUNE_GONE_LIBRARIES is on, are the items of each that Plex
        still does not list an hour later. No scan of a listed library would, and
        while they are stored they hold back the deletion of what is at their
        paths. One look is not enough, however few the items: an answer may lack
        a library that is there. An empty list of libraries says nothing, and one
        Plex failed to give ends the run before it starts. A library that is only
        unticked in Settings keeps its items.
        """
        if not self._library_types:
            return 0
        begin_write(self._conn)
        before = self._read_gone_since()
        ids = fetch_gone_library_ids(self._conn, self._library_types)
        gone = {lib_id: before.get(lib_id) or now_iso() for lib_id in ids}
        if gone != before:
            logger.warning("engine.gone_libraries now=%s before=%s", gone, before)
            self._conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (_GONE_LIBRARIES_KEY, json.dumps(gone), now_iso()),
            )
        if not _PRUNE_GONE_LIBRARIES:
            return 0
        removed = 0
        for lib_id, since in gone.items():
            if not self._began_after(since, _GONE_FOR):
                continue
            listed = self._listed_keys.intersection(
                repository.fetch_ids_in_libraries(self._conn, [lib_id])
            )
            removed += self._remove_orphaned_items(listed, {lib_id}, confirmed=True)
        return removed

    def _read_gone_since(self) -> dict[int, str]:
        """Return when each library was first found gone; a record that cannot be read is none."""
        try:
            stored = json.loads(repository.read_setting(self._conn, _GONE_LIBRARIES_KEY) or "{}")
        except ValueError:
            return {}
        if not isinstance(stored, dict):
            return {}
        return {
            int(lib_id): since
            for lib_id, since in stored.items()
            if lib_id.isdecimal() and self._is_past(since)
        }

    @staticmethod
    def _is_past(moment: object) -> bool:
        parsed = _parse_iso_utc(moment) if isinstance(moment, str) else None
        return parsed is not None and parsed <= now_utc()

    def _began_after(self, moment: str, delay: timedelta) -> bool:
        """Return True if the current run began at least *delay* after *moment*.

        What it lists was read after it began, so it is a look of its own.
        """
        began, earlier = _parse_iso_utc(self._started_at), _parse_iso_utc(moment)
        return began is not None and earlier is not None and began - earlier >= delay

    def _remove_orphaned_items(
        self,
        seen_keys: set[str],
        scanned_libs: set[int],
        *,
        confirmed: bool = False,
    ) -> int:
        """Remove ``media_items`` whose ``plex_rating_key`` is gone from Plex.

        Delegates to phases.delete.remove_orphans which owns the
        fail-closed safeguard logic. Only considers items belonging
        to libraries that were successfully scanned.
        """
        return remove_orphans(
            self._conn,
            seen_keys,
            scanned_libs,
            listed_since=self._started_at,
            confirmed=confirmed,
        )
