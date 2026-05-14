"""Per-library scan helpers — extracted from :mod:`engine` so the
orchestrator stays under the 500-line file ceiling.

These helpers are *module-level functions* that take the
:class:`mediaman.scanner.engine.ScanEngine` as their first argument and
read/write its state through attributes. They are not free functions
in the API sense — they are the engine's per-library body lifted out
of the class so the orchestration shell remains scannable.

The per-library transaction discipline is unchanged: the orchestrator
in :meth:`ScanEngine.run_scan` issues a ``self._conn.commit()`` after
each library's helper returns, so a SIGKILL mid-scan can still only
roll back the in-flight library. The helpers themselves do not commit.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from mediaman.core.time import now_iso
from mediaman.scanner import repository
from mediaman.scanner.fetch import PlexItemFetch
from mediaman.scanner.phases.evaluate import evaluate_movie, evaluate_season
from mediaman.scanner.phases.upsert import schedule_deletion as _phase_schedule_deletion
from mediaman.scanner.phases.upsert import upsert_item as _phase_upsert_item

if TYPE_CHECKING:
    from mediaman.scanner.engine import ScanEngine

logger = logging.getLogger(__name__)

__all__ = ["scan_items", "scan_movie_library", "scan_tv_library"]


@dataclass(slots=True)
class _GuardSets:
    """Per-library protection / already-scheduled lookup sets.

    Built once per library by :func:`_build_guard_sets` so the hot
    per-item loop does ZERO ``scheduled_actions`` SELECTs for the
    protection and already-scheduled guards (§13.3 — the previous code
    issued up to three of those SELECTs per item). ``already_scheduled``
    is mutable: when an item is freshly scheduled mid-loop it is added so
    a later item sharing the same ``plex_rating_key`` observes it, exactly
    as the per-item ``is_already_scheduled`` query did within the
    connection's uncommitted view.
    """

    protected: frozenset[str]
    already_scheduled: set[str]


def _build_guard_sets(conn: sqlite3.Connection, media_ids: list[str]) -> _GuardSets:
    """Batch-load the protection and already-scheduled sets for a library.

    Replaces the per-item :func:`repository.is_protected` and
    :func:`repository.is_already_scheduled` round trips with two
    ``IN (...)`` queries (§13.3). The active-ness rules in the two
    repository helpers match the per-item predicates byte-for-byte, so
    the protection decision is identical before and after batching.
    """
    return _GuardSets(
        protected=frozenset(repository.fetch_protected_media_ids(conn, media_ids, now_iso())),
        already_scheduled=repository.fetch_already_scheduled_media_ids(conn, media_ids),
    )


def scan_items(
    engine: ScanEngine,
    fetched: list[PlexItemFetch],
    media_type_fn: Callable[[PlexItemFetch], str],
    evaluate_fn: Callable[[PlexItemFetch, datetime, Sequence[Mapping[str, object]]], str | None],
    item_label: str,
    library_id: str,
    summary: dict[str, int],
    seen_keys: set[str] | None = None,
) -> None:
    """Shared iteration skeleton for movie and TV scan passes.

    Builds the per-library protection / already-scheduled guard sets in
    two batched queries up front (§13.3), then iterates *fetched* items,
    upserts each one, applies the common protection/schedule guards via
    :func:`_evaluate_scan_item`, then routes the per-item ``decision``
    through :func:`_apply_scan_decision`. The two callers differ only in
    *media_type_fn* (selects the media type string) and *evaluate_fn*
    (returns ``"schedule_deletion"``, a skip marker, or ``None`` for an
    evaluator-driven early skip such as the TV show-kept check).

    *library_id* is reserved for future use — kept in the signature to
    mirror the per-library shape of the callers and avoid churn in the
    rest of the call graph. *seen_keys*, when provided, accumulates Plex
    rating keys so orphan detection can exclude them later.
    """
    del library_id  # reserved; see docstring
    # §13.3: one batched query per guard, built before the loop — the
    # protection state of an item cannot change under the per-item
    # ``media_items`` upserts, so a single up-front read is identical to
    # the old per-item SELECTs.
    media_ids = [f.item["plex_rating_key"] for f in fetched]
    guards = _build_guard_sets(engine._conn, media_ids)
    for f in fetched:
        summary["scanned"] += 1
        try:
            media_id, decision = _evaluate_scan_item(
                engine, f, media_type_fn, evaluate_fn, seen_keys, guards
            )
            if decision is None or decision == _SKIP:
                # ``None`` = evaluator early-skip (e.g. show-kept);
                # ``_SKIP`` = guard-driven skip (protected / already scheduled).
                summary["skipped"] += 1
                continue
            _apply_scan_decision(engine, media_id, decision, summary, guards)
        except Exception:  # rationale: per-item isolation boundary — a single corrupt or malformed item cannot abort the entire library scan.
            # Errors are recorded in summary["errors"] and logged with full
            # traceback so operators can diagnose the root cause; the scheduler-level
            # wrapper in runner.py handles job-level failures.
            summary["errors"] += 1
            logger.exception(
                "%s scan item failed (plex_rating_key=%s)",
                item_label,
                f.item.get("plex_rating_key", "?"),
            )


# Sentinel returned by _evaluate_scan_item when a guard short-circuits
# evaluation. Kept distinct from None (evaluator early-skip) and from any
# real evaluator string so scan_items can distinguish the two skip paths.
_SKIP = "_skip"


def _evaluate_scan_item(
    engine: ScanEngine,
    f: PlexItemFetch,
    media_type_fn: Callable[[PlexItemFetch], str],
    evaluate_fn: Callable[[PlexItemFetch, datetime, Sequence[Mapping[str, object]]], str | None],
    seen_keys: set[str] | None,
    guards: _GuardSets,
) -> tuple[str, str | None]:
    """Upsert one item and return ``(media_id, decision)``.

    ``decision`` is the evaluator's return value
    (``"schedule_deletion"``, a skip marker, or ``None`` for an
    evaluator-driven early skip such as the TV show-kept check). When
    the protection / already-scheduled guards fire we return
    ``(media_id, _SKIP)`` so the caller bumps the skipped counter
    without invoking the evaluator.

    The protection and already-scheduled guards are answered from the
    pre-built *guards* sets — no per-item ``scheduled_actions`` SELECT
    (§13.3). The ``media_items`` upsert below cannot change either
    guard's answer because neither guard reads ``media_items``.
    """
    conn = engine._conn
    item = f.item
    media_id = item["plex_rating_key"]
    if seen_keys is not None:
        seen_keys.add(media_id)
    _phase_upsert_item(conn, f, engine._arr_cache, media_type_fn(f))
    repository.update_last_watched(conn, media_id, f.watch_history)

    if media_id in guards.protected:
        return media_id, _SKIP
    if media_id in guards.already_scheduled:
        return media_id, _SKIP

    added_at = engine._resolve_added_at(cast(dict[str, object], item))
    decision = evaluate_fn(f, added_at, f.watch_history)
    return media_id, decision


def _apply_scan_decision(
    engine: ScanEngine,
    media_id: str,
    decision: str,
    summary: dict[str, int],
    guards: _GuardSets,
) -> None:
    """Apply a per-item decision (``schedule_deletion`` or skip).

    Branches on dry-run mode and re-entry status; bumps the matching
    summary counter. Any decision other than ``"schedule_deletion"`` is
    treated as a skip so the caller doesn't have to duplicate the
    bookkeeping.

    When an item is freshly scheduled, its id is added to
    *guards.already_scheduled* so a later *fetched* item sharing the
    same ``plex_rating_key`` is treated as already-scheduled — the same
    answer the old per-item ``is_already_scheduled`` query gave from the
    connection's uncommitted view.
    """
    if decision != "schedule_deletion":
        summary["skipped"] += 1
        return
    if engine._dry_run:
        # Dry-run preview: count what *would* be scheduled but write
        # nothing. Both ``scheduled_actions`` and the audit_log row
        # inside ``schedule_deletion`` are skipped.
        summary["scheduled"] += 1
        return
    is_reentry = repository.has_expired_snooze(engine._conn, media_id)
    _phase_schedule_deletion(
        engine._conn,
        media_id=media_id,
        is_reentry=is_reentry,
        grace_days=engine._grace_days,
        secret_key=engine._secret_key,
    )
    guards.already_scheduled.add(media_id)
    summary["scheduled"] += 1


def scan_movie_library(
    engine: ScanEngine,
    library_id: str,
    summary: dict[str, int],
    seen_keys: set[str] | None = None,
) -> None:
    """Scan a single movie library: fetch, upsert, evaluate, schedule."""
    # Phase 1: pull items + watch histories from Plex (no DB writes,
    # no lock). See :meth:`ScanEngine.sync_library` for rationale.
    fetched = engine._fetcher.fetch_library_items(library_id)

    def _evaluate(
        f: PlexItemFetch,
        added_at: datetime,
        watch_history: Sequence[Mapping[str, object]],
    ) -> str | None:
        return evaluate_movie(
            added_at=added_at,
            watch_history=watch_history,
            min_age_days=engine._min_age_days,
            inactivity_days=engine._inactivity_days,
        )

    scan_items(
        engine,
        fetched,
        media_type_fn=lambda f: "movie",
        evaluate_fn=_evaluate,
        item_label="Movie",
        library_id=library_id,
        summary=summary,
        seen_keys=seen_keys,
    )


def scan_tv_library(
    engine: ScanEngine,
    library_id: str,
    summary: dict[str, int],
    seen_keys: set[str] | None = None,
) -> None:
    """Scan a single TV library: fetch, upsert, evaluate, schedule.

    Honours the show-kept protection: every season belonging to a kept
    show is skipped before evaluation.
    """
    # Phase 1: network fetch (see :meth:`ScanEngine.sync_library`).
    fetched = engine._fetcher.fetch_library_items(library_id)
    conn = engine._conn

    def _evaluate(
        f: PlexItemFetch,
        added_at: datetime,
        watch_history: Sequence[Mapping[str, object]],
    ) -> str | None:
        season = f.item
        raw_key = season.get("show_rating_key")
        show_key = raw_key if isinstance(raw_key, str) else None
        if repository.is_show_kept(conn, show_key):
            return None  # show is protected; skip all its seasons
        return evaluate_season(
            added_at=added_at,
            watch_history=watch_history,
            min_age_days=engine._min_age_days,
            inactivity_days=engine._inactivity_days,
        )

    scan_items(
        engine,
        fetched,
        media_type_fn=lambda f: f.media_type,
        evaluate_fn=_evaluate,
        item_label="TV",
        library_id=library_id,
        summary=summary,
        seen_keys=seen_keys,
    )
