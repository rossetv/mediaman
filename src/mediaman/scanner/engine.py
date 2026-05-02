"""Scan engine — orchestrates a full Plex library scan.

For each configured library the engine:

1. Fetches all items from Plex (via :mod:`fetch`).
2. Upserts each item into ``media_items`` (via :mod:`repository`).
3. Skips items that are protected (forever or active snooze).
4. Skips items already awaiting deletion.
5. Evaluates eligibility via ``evaluate_movie`` / ``evaluate_season``.
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

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Callable

from mediaman.scanner import repository
from mediaman.scanner.arr_dates import ArrDateCache
from mediaman.scanner.deletions import (
    DeletionExecutor,
    _recover_stuck_deletions,
)
from mediaman.scanner.fetch import PlexFetcher, _PlexItemFetch
from mediaman.scanner.movies import evaluate_movie
from mediaman.scanner.tv import evaluate_season
from mediaman.services.infra.format import ensure_tz as _ensure_tz
from mediaman.services.infra.format import parse_iso_utc as _parse_iso_utc
from mediaman.services.infra.settings_reader import get_bool_setting as _get_bool_setting
from mediaman.services.infra.storage import delete_path  # re-exported for back-compat
from mediaman.services.mail.newsletter import send_newsletter as _send_newsletter
from mediaman.services.openai.recommendations.persist import (
    refresh_recommendations as _refresh_recommendations,
)

logger = logging.getLogger("mediaman")

# Back-compat: callers historically imported these from engine.
__all__ = [
    "ScanEngine",
    "_PlexItemFetch",
    "_recover_stuck_deletions",
    "delete_path",
]


def _coerce_lib_ids(raw: Iterable[str]) -> set[int]:
    """Coerce an iterable of library ID strings to a set of ints, skipping invalid values.

    Malformed settings values (e.g. ``"all"``, empty strings) are silently
    skipped with a warning so a single bad entry cannot abort orphan cleanup
    for every other library.
    """
    result: set[int] = set()
    for lib_id in raw:
        try:
            result.add(int(lib_id))
        except (ValueError, TypeError):
            logger.warning(
                "Ignoring non-integer library id %r in orphan cleanup",
                lib_id,
            )
    return result


# Orphan-removal safeguards (C31). A scan that finds zero items against
# a previously-populated library is almost always a Plex auth hiccup,
# not a genuine mass-deletion. Refuse to treat such a result as
# authoritative.
_MIN_ITEMS_TO_TRUST = 5
_MIN_ITEMS_FOR_RATIO_CHECK = 50
_MIN_RATIO_TO_TRUST = 0.10


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
            in the deletion executor are also skipped (delegated to
            ``skip_rmtree`` below). Use this for "what would happen"
            previews. Defaults to False.
        skip_rmtree: When True, prevents the deletion executor from
            invoking ``delete_path`` (the on-disk rm) but allows every
            other write — schedule_deletion, orphan cleanup, newsletter,
            and audit logging still run. ``dry_run=True`` implies
            ``skip_rmtree=True``. Use ``skip_rmtree`` directly when you
            want the narrower "no rm" behaviour without disabling the
            rest of the pipeline. Defaults to False.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        plex_client: Any,
        library_ids: list[str],
        library_types: dict[str, str],
        library_titles: dict[str, str] | None = None,
        secret_key: str,
        min_age_days: int = 30,
        inactivity_days: int = 30,
        grace_days: int = 14,
        dry_run: bool = False,
        skip_rmtree: bool = False,
        sonarr_client: Any = None,
        radarr_client: Any = None,
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
        # dry_run implies skip_rmtree — a true preview cannot perform
        # on-disk deletions either.
        self._skip_rmtree = bool(skip_rmtree or dry_run)
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
            dry_run=self._skip_rmtree,
            cleanup_snoozes=not dry_run,
            sonarr_client=sonarr_client,
            radarr_client=radarr_client,
        )

    # ------------------------------------------------------------------
    # Back-compat shims — keep the pre-split private API importable so
    # the rest of the codebase (and tests) keep working without a
    # behavioural change.
    # ------------------------------------------------------------------

    def _load_delete_allowed_roots(self) -> list[str]:
        return repository.read_delete_allowed_roots_setting(self._conn)

    def _ensure_arr_dates(self) -> None:
        self._arr_cache.ensure_loaded()

    def _build_arr_date_cache(self) -> None:  # pragma: no cover — compat shim
        # Forces a (re)load. Only used as an explicit helper in tests.
        self._arr_cache.reset()
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
           (D05 finding 5) so the SQLite write lock is held only for
           a single library's worth of writes at a time.
        """
        summary = {"synced": 0, "errors": 0, "removed": 0}

        # Phase 1: network reads. No DB writes, no lock.
        # Group fetches by library so phase 2 can commit per library —
        # an OOM kill mid-scan won't roll back already-synced libraries.
        per_lib_fetches: dict[str, list[_PlexItemFetch]] = {}
        successfully_scanned: list[str] = []
        for lib_id in self._library_ids:
            try:
                per_lib_fetches[lib_id] = self._fetcher.fetch_library_items(lib_id)
                successfully_scanned.append(lib_id)
            except Exception:
                logger.exception("Library sync failed for library %s", lib_id)
                summary["errors"] += 1

        # Phase 2: per-library write transactions. No network calls
        # happen past this point, so the write lock is held only for
        # one library's UPSERTs at a time.
        for lib_id in successfully_scanned:
            fetches = per_lib_fetches.get(lib_id, [])
            seen_lib_keys: set[str] = {f.item["plex_rating_key"] for f in fetches}
            for f in fetches:
                self._upsert_media_item(f.item, f.library_id, f.media_type)
                if f.watch_history:
                    repository.update_last_watched(
                        self._conn, f.item["plex_rating_key"], f.watch_history
                    )
                summary["synced"] += 1
            # Per-library orphan cleanup so an empty result on one
            # library cannot wipe items belonging to another.
            summary["removed"] += self._remove_orphaned_items(
                seen_lib_keys,
                _coerce_lib_ids([lib_id]),
            )
            self._conn.commit()

        logger.info(
            "Library sync complete: %d synced, %d orphans removed, %d errors",
            summary["synced"],
            summary["removed"],
            summary["errors"],
        )
        return summary

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
        # Per-library accumulator so the orphan safeguard is evaluated
        # against each library independently (D05 finding 7).
        seen_by_lib: dict[str, set[str]] = {}

        for lib_id in self._library_ids:
            lib_type = self._library_types.get(lib_id)
            if lib_type is None:
                # An unmapped library would silently default to "movie",
                # so a Plex library that has been re-typed (e.g. moved to
                # "music" or "photo") would be scanned as if it were a
                # movie collection. Skip loudly instead.
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
                self._scan_tv_library(lib_id, summary, seen)
            else:
                self._scan_movie_library(lib_id, summary, seen)

            # Per-library commit (D05 finding 4 + 5): bound the SQLite
            # write-lock duration so a SIGKILL mid-scan can only roll
            # back the in-flight library, not every successful upsert
            # from earlier libraries.
            self._conn.commit()

        # Per-library orphan cleanup so a single library's empty result
        # cannot wipe items belonging to another library.
        if self._dry_run:
            logger.info("engine.run_scan.dry_run skipping orphan removal")
        else:
            for lib_id, seen in seen_by_lib.items():
                lib_set = _coerce_lib_ids([lib_id])
                summary["removed"] += self._remove_orphaned_items(seen, lib_set)
            self._conn.commit()

        deletion_result = self.execute_deletions()
        summary["deleted"] = deletion_result["deleted"]
        summary["reclaimed_bytes"] = deletion_result["reclaimed_bytes"]

        if self._dry_run:
            logger.info("engine.run_scan.dry_run skipping newsletter + recommendations refresh")
        else:
            try:
                _send_newsletter(
                    conn=self._conn,
                    secret_key=self._secret_key,
                    dry_run=self._dry_run,
                    grace_days=self._grace_days,
                )
            except Exception:
                logger.exception("Newsletter sending failed — scan results unaffected")

            # Refresh AI recommendations if enabled.
            if _get_bool_setting(self._conn, "suggestions_enabled", default=True):
                try:
                    _refresh_recommendations(self._conn, self._plex, secret_key=self._secret_key)
                except Exception:
                    logger.exception("Recommendation generation failed — scan results unaffected")

        return summary

    def execute_deletions(self) -> dict[str, int]:
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
        arr_date_str = self._arr_cache.get(item.get("file_path", ""))
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
        return _ensure_tz(item.get("added_at") or item.get("updated_at"))

    def _scan_items(
        self,
        fetched: list[_PlexItemFetch],
        media_type_fn: Callable[[_PlexItemFetch], str],
        evaluate_fn: Callable[[_PlexItemFetch, Any, list[dict[str, object]]], str | None],
        item_label: str,
        library_id: str,
        summary: dict[str, int],
        seen_keys: set[str] | None = None,
    ) -> None:
        """Shared iteration skeleton for movie and TV scan passes.

        Iterates *fetched* items, upserts each one, applies the common
        protection/schedule guards, then delegates per-item evaluation
        to *evaluate_fn*. The two callers differ only in
        ``media_type_fn`` (which selects the media type string from the
        fetch record) and ``evaluate_fn`` (which calls the appropriate
        evaluator and applies any domain-specific pre-skip logic such
        as the TV show-kept check).

        Args:
            fetched: Pre-fetched items from :meth:`_fetch_library_items`.
            media_type_fn: Callable that returns the media_type string
                for a :class:`_PlexItemFetch` record.
            evaluate_fn: Callable ``(fetch, added_at, watch_history) ->
                decision`` where decision is ``"schedule_deletion"`` or
                any other value meaning "skip". May return ``None`` to
                signal an early skip (e.g. show-kept check failed before
                evaluation).
            item_label: Human-readable label used in exception log
                messages (e.g. ``"Movie"`` or ``"TV"``).
            library_id: Plex section ID — passed through to
                :meth:`_upsert_media_item`.
            summary: Mutable summary counter dict (``scanned``,
                ``scheduled``, ``skipped``, ``errors``).
            seen_keys: If provided, the item's Plex rating key is added
                so orphan detection can exclude it later.
        """
        for f in fetched:
            summary["scanned"] += 1
            item = f.item
            watch_history = f.watch_history
            try:
                media_id = item["plex_rating_key"]
                if seen_keys is not None:
                    seen_keys.add(media_id)
                self._upsert_media_item(item, library_id, media_type_fn(f))
                repository.update_last_watched(self._conn, media_id, watch_history)

                if repository.is_protected(self._conn, media_id):
                    summary["skipped"] += 1
                    continue

                if repository.is_already_scheduled(self._conn, media_id):
                    summary["skipped"] += 1
                    continue

                added_at = self._resolve_added_at(item)
                decision = evaluate_fn(f, added_at, watch_history)

                if decision is None:
                    # evaluate_fn signalled an early skip (e.g. show-kept).
                    summary["skipped"] += 1
                    continue

                if decision == "schedule_deletion":
                    if self._dry_run:
                        # Dry-run preview: count what *would* be scheduled
                        # but write nothing. Both ``scheduled_actions`` and
                        # the audit_log row inside ``schedule_deletion``
                        # are skipped.
                        summary["scheduled"] += 1
                    else:
                        is_reentry = repository.has_expired_snooze(self._conn, media_id)
                        repository.schedule_deletion(
                            self._conn,
                            media_id=media_id,
                            is_reentry=is_reentry,
                            grace_days=self._grace_days,
                            secret_key=self._secret_key,
                        )
                        summary["scheduled"] += 1
                else:
                    summary["skipped"] += 1
            except Exception:
                summary["errors"] += 1
                logger.exception(
                    "%s scan item failed (plex_rating_key=%s)",
                    item_label,
                    item.get("plex_rating_key", "?"),
                )

    def _scan_movie_library(
        self,
        library_id: str,
        summary: dict[str, int],
        seen_keys: set[str] | None = None,
    ) -> None:
        # Phase 1: pull items + watch histories from Plex (no DB writes,
        # no lock). See :meth:`sync_library` for rationale.
        fetched = self._fetcher.fetch_library_items(library_id)

        def _evaluate(
            f: _PlexItemFetch,
            added_at: Any,
            watch_history: list[dict[str, object]],
        ) -> str | None:
            return evaluate_movie(
                added_at=added_at,
                watch_history=watch_history,
                min_age_days=self._min_age_days,
                inactivity_days=self._inactivity_days,
            )

        self._scan_items(
            fetched,
            media_type_fn=lambda f: "movie",
            evaluate_fn=_evaluate,
            item_label="Movie",
            library_id=library_id,
            summary=summary,
            seen_keys=seen_keys,
        )

    def _scan_tv_library(
        self,
        library_id: str,
        summary: dict[str, int],
        seen_keys: set[str] | None = None,
    ) -> None:
        # Phase 1: network fetch (see :meth:`sync_library`).
        fetched = self._fetcher.fetch_library_items(library_id)

        # The show-kept check is TV-specific; returning None from
        # evaluate_fn signals the skeleton to count this item as skipped
        # without scheduling.
        def _evaluate(
            f: _PlexItemFetch,
            added_at: Any,
            watch_history: list[dict[str, object]],
        ) -> str | None:
            season = f.item
            if repository.is_show_kept(self._conn, season.get("show_rating_key")):
                return None  # signals early skip to _scan_items
            return evaluate_season(
                added_at=added_at,
                episode_count=season.get("episode_count", 0),
                watch_history=watch_history,
                has_future_episodes=False,
                min_age_days=self._min_age_days,
                inactivity_days=self._inactivity_days,
            )

        self._scan_items(
            fetched,
            media_type_fn=lambda f: f.media_type,
            evaluate_fn=_evaluate,
            item_label="TV",
            library_id=library_id,
            summary=summary,
            seen_keys=seen_keys,
        )

    def _remove_orphaned_items(
        self,
        seen_keys: set[str],
        scanned_libs: set[int],
    ) -> int:
        """Remove ``media_items`` whose ``plex_rating_key`` is gone from Plex.

        Only considers items belonging to libraries that were
        successfully scanned (so we don't accidentally delete items from
        a library that was unreachable during this sync).

        Fail-closed safeguards against a Plex auth hiccup returning zero
        items and wiping every kept-state / snooze row:

        * If the scan found fewer than ``_MIN_ITEMS_TO_TRUST`` items in
          total across the scanned libraries, we refuse to treat the
          result as authoritative and skip orphan removal.
        * If the scan found less than ``_MIN_RATIO_TO_TRUST`` (10 %) of
          the items we had on record for those libraries, likewise.

        In both cases a warning is logged with the exact numbers so an
        admin can investigate and, if the result really was correct,
        reconcile manually.
        """
        if not scanned_libs:
            return 0

        previous_count = repository.count_items_in_libraries(self._conn, list(scanned_libs))
        current_count = len(seen_keys)

        # Hard floor: fewer than this many items makes the scan look
        # like a failure mode (e.g. Plex auth hiccup returning empty).
        if current_count < _MIN_ITEMS_TO_TRUST and previous_count >= _MIN_ITEMS_TO_TRUST:
            logger.warning(
                "engine.orphan_guard.skip reason=below_min_items "
                "current=%d previous=%d threshold=%d scanned_libs=%s — "
                "refusing to remove orphans; admin must verify and "
                "reconcile manually if this is correct.",
                current_count,
                previous_count,
                _MIN_ITEMS_TO_TRUST,
                sorted(scanned_libs),
            )
            return 0

        # Fractional floor: a huge drop between runs is also suspicious.
        if (
            previous_count > _MIN_ITEMS_FOR_RATIO_CHECK
            and current_count < previous_count * _MIN_RATIO_TO_TRUST
        ):
            logger.warning(
                "engine.orphan_guard.skip reason=below_ratio "
                "current=%d previous=%d ratio=%.3f min_ratio=%.2f "
                "scanned_libs=%s — refusing to remove orphans; admin "
                "must verify and reconcile manually if this is correct.",
                current_count,
                previous_count,
                (current_count / previous_count) if previous_count else 0.0,
                _MIN_RATIO_TO_TRUST,
                sorted(scanned_libs),
            )
            return 0

        all_ids = repository.fetch_ids_in_libraries(self._conn, list(scanned_libs))
        orphan_ids = [i for i in all_ids if i not in seen_keys]

        if not orphan_ids:
            return 0

        repository.delete_media_items(self._conn, orphan_ids)
        logger.info(
            "Removed %d orphaned media items no longer in Plex",
            len(orphan_ids),
        )
        return len(orphan_ids)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _upsert_media_item(
        self,
        item: dict[str, object],
        library_id: str,
        media_type: str,
    ) -> None:
        """Insert or update a media item record.

        Kept as a method (not inlined) because it accesses ``self._arr_cache``
        before delegating to :func:`repository.upsert_media_item` — it is not
        a pure pass-through.
        """
        self._arr_cache.ensure_loaded()
        arr_date = self._arr_cache.get(item.get("file_path", ""))
        repository.upsert_media_item(
            self._conn,
            item=item,
            library_id=library_id,
            media_type=media_type,
            arr_date=arr_date,
        )
