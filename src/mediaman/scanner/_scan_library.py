"""Per-library scan helpers, kept out of engine so the
orchestrator stays small.

These helpers are *module-level functions* that take the
mediaman.scanner.engine.ScanEngine as their first argument and
read/write its state through attributes. They are not free functions
in the API sense — they are the engine's per-library body, kept out
of the class so the orchestration shell remains scannable.

The per-library transaction discipline is unchanged: the orchestrator
in ScanEngine.run_scan issues a ``self._conn.commit()`` after
each library's helper returns, so a SIGKILL mid-scan can still only
roll back the in-flight library. The helpers themselves do not commit.
"""

from __future__ import annotations

import enum
import logging
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, cast

from mediaman.core.time import now_iso
from mediaman.scanner import repository
from mediaman.scanner._eligibility import is_inactive
from mediaman.scanner.fetch import PlexItemFetch
from mediaman.scanner.phases.evaluate import evaluate_item
from mediaman.scanner.phases.upsert import schedule_deletion as _phase_schedule_deletion
from mediaman.scanner.phases.upsert import upsert_item as _phase_upsert_item
from mediaman.scanner.repository.watch_credit import last_watched_by_media_item

if TYPE_CHECKING:
    from mediaman.scanner.engine import ScanEngine

logger = logging.getLogger(__name__)

__all__ = [
    "ScanDecision",
    "item_savepoint",
    "scan_items",
    "scan_movie_library",
    "scan_tv_library",
    "with_mediaman_play",
]


def with_mediaman_play(f: PlexItemFetch, credited: Mapping[str, datetime]) -> PlexItemFetch:
    """Return *f* with its latest mediaman play added to its Plex watch history.

    Shared by the cleanup scan and the library sync so both advance
    ``last_watched_at`` from the same plays.
    """
    credited_at = credited.get(f.item["plex_rating_key"])
    if credited_at is None:
        return f
    return replace(f, watch_history=[*f.watch_history, {"viewed_at": credited_at}])


@contextmanager
def item_savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """Roll back only this item's writes when the body raises, so none is left half-written.

    Shared by the cleanup scan and the library sync. A fatal SQLite error (disk
    full, I/O) ends the transaction and the savepoint with it; the job's own
    rollback still runs.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT scan_item")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO scan_item")
            conn.execute("RELEASE scan_item")
        raise
    if conn.in_transaction:
        conn.execute("RELEASE scan_item")


class ScanDecision(enum.Enum):
    """The three explicit outcomes of evaluating one scanned item.

    Explicit members, rather than strings or ``None``, so a guard skip, an
    evaluator skip and a schedule can never be confused.

    * SCHEDULE — the item is eligible; schedule a deletion.
    * SKIP_GUARD — a protection / already-scheduled guard fired
      before the evaluator ran.
    * SKIP_EVAL — the evaluator declined (too new, watched
      recently, or an evaluator early-skip such as a kept show).
    """

    SCHEDULE = "schedule"
    SKIP_GUARD = "skip_guard"
    SKIP_EVAL = "skip_eval"


@dataclass(slots=True)
class _GuardSets:
    """Per-library protection / already-scheduled / mediaman-play lookups.

    Built once per library by _build_guard_sets so the hot
    per-item loop does ZERO ``scheduled_actions`` or ``watch_progress``
    SELECTs. ``credited`` maps a media item id to the last time anyone
    played it in mediaman long enough to count as a watch, and
    ``last_watched`` to the ``last_watched_at`` stored before this scan.
    ``already_scheduled`` is mutable: when an item is freshly scheduled mid-loop it is added so
    a later item sharing the same ``plex_rating_key`` observes it, as the
    connection's uncommitted view would.
    """

    protected: frozenset[str]
    already_scheduled: set[str]
    credited: Mapping[str, datetime]
    last_watched: Mapping[str, datetime]


def _build_guard_sets(conn: sqlite3.Connection, media_ids: list[str]) -> _GuardSets:
    """Batch-load the guard sets and mediaman play credits for a library.

    Two ``IN (...)`` queries instead of one repository.is_protected and
    repository.is_already_scheduled round trip per item. The active-ness
    rules in the two repository helpers match the per-item predicates
    byte-for-byte.
    """
    return _GuardSets(
        protected=frozenset(repository.fetch_protected_media_ids(conn, media_ids, now_iso())),
        already_scheduled=repository.fetch_already_scheduled_media_ids(conn, media_ids),
        credited=repository.last_credited_by_media_item(conn, media_ids),
        last_watched=last_watched_by_media_item(conn, media_ids),
    )


def scan_items(
    engine: ScanEngine,
    fetched: list[PlexItemFetch],
    media_type_fn: Callable[[PlexItemFetch], str],
    evaluate_fn: Callable[[PlexItemFetch, datetime, Sequence[Mapping[str, object]]], str | None],
    item_label: str,
    summary: dict[str, int],
    seen_keys: set[str] | None = None,
) -> None:
    """Shared iteration skeleton for movie and TV scan passes.

    Builds the per-library protection / already-scheduled guard sets in
    two batched queries up front, then iterates *fetched* items,
    upserts each one, applies the common protection/schedule guards via
    _evaluate_scan_item, then routes the per-item ``decision``
    through _apply_scan_decision. The two callers differ only in
    *media_type_fn* (selects the media type string) and *evaluate_fn*
    (returns ``"schedule_deletion"``, a skip marker, or ``None`` for an
    evaluator-driven early skip such as the TV show-kept check).

    *seen_keys*, when provided, accumulates Plex rating keys so orphan
    detection can exclude them later.
    """
    # One batched query per guard, built before the loop — the
    # protection state of an item cannot change under the per-item
    # ``media_items`` upserts, so a single up-front read gives the
    # same answer as a read per item.
    media_ids = [f.item["plex_rating_key"] for f in fetched]
    guards = _build_guard_sets(engine._conn, media_ids)
    for f in fetched:
        summary["scanned"] += 1
        try:
            with item_savepoint(engine._conn):
                media_id, decision = _evaluate_scan_item(
                    engine, f, media_type_fn, evaluate_fn, seen_keys, guards
                )
                if decision is not ScanDecision.SCHEDULE:
                    # SKIP_GUARD (protected / already scheduled) or SKIP_EVAL
                    # (too new / watched recently / kept show) — count and move on.
                    summary["skipped"] += 1
                    continue
                _apply_scan_decision(engine, media_id, summary, guards)
        except (KeyError, TypeError, ValueError, RuntimeError, sqlite3.Error):
            # rationale: per-item isolation boundary, narrowed to the
            # exception types a corrupt/malformed Plex item or a transient
            # DB error can raise — a missing key, a wrong-typed field, an
            # unparseable value, a Plex-client RuntimeError, or a SQLite
            # error. A genuine programming bug (AttributeError, NameError,
            # ...) is deliberately NOT caught here so it propagates and is
            # not silently masked as an "item error". Caught errors are
            # recorded in summary["errors"] and logged with full traceback
            # so operators can diagnose the root cause; the scheduler-level
            # wrapper in runner.py handles job-level failures.
            summary["errors"] += 1
            logger.exception(
                "%s scan item failed (plex_rating_key=%s)",
                item_label,
                f.item.get("plex_rating_key", "?"),
            )


def _evaluate_scan_item(
    engine: ScanEngine,
    f: PlexItemFetch,
    media_type_fn: Callable[[PlexItemFetch], str],
    evaluate_fn: Callable[[PlexItemFetch, datetime, Sequence[Mapping[str, object]]], str | None],
    seen_keys: set[str] | None,
    guards: _GuardSets,
) -> tuple[str, ScanDecision]:
    """Upsert one item and return ``(media_id, decision)``.

    The returned ScanDecision makes the three outcomes explicit:
    ScanDecision.SKIP_GUARD when the protection / already-scheduled
    guards fire (no evaluator call), ScanDecision.SCHEDULE when the
    evaluator returns ``"schedule_deletion"``, and
    ScanDecision.SKIP_EVAL for any other evaluator result
    (``"skip"`` or ``None`` — e.g. the TV show-kept early skip).

    The protection and already-scheduled guards are answered from the
    pre-built *guards* sets — no per-item ``scheduled_actions`` SELECT.
    The ``media_items`` upsert below cannot change either guard's answer
    because neither guard reads ``media_items``.

    A mediaman play credited to the item joins its Plex watch history, so
    both ``last_watched_at`` and the inactivity check count it. The stored
    ``last_watched_at`` is a floor for the inactivity check, so a watch
    whose play is gone (its viewer deleted) still keeps the item.
    """
    conn = engine._conn
    item = f.item
    media_id = item["plex_rating_key"]
    if seen_keys is not None:
        seen_keys.add(media_id)
    f = with_mediaman_play(f, guards.credited)
    _phase_upsert_item(conn, f, engine._arr_cache, media_type_fn(f))

    if media_id in guards.protected:
        return media_id, ScanDecision.SKIP_GUARD
    if media_id in guards.already_scheduled:
        return media_id, ScanDecision.SKIP_GUARD

    added_at = engine._resolve_added_at(cast(dict[str, object], item))
    if evaluate_fn(f, added_at, f.watch_history) != "schedule_deletion":
        return media_id, ScanDecision.SKIP_EVAL
    last_watched = guards.last_watched.get(media_id)
    if last_watched is not None and not is_inactive(
        [{"viewed_at": last_watched}], engine._inactivity_days
    ):
        return media_id, ScanDecision.SKIP_EVAL
    return media_id, ScanDecision.SCHEDULE


def _apply_scan_decision(
    engine: ScanEngine,
    media_id: str,
    summary: dict[str, int],
    guards: _GuardSets,
) -> None:
    """Schedule a deletion for *media_id* and bump the summary counter.

    Only ever called for a ScanDecision.SCHEDULE item — skips are
    handled by the caller — so this branches solely on dry-run mode and
    re-entry status.

    When an item is freshly scheduled, its id is added to
    *guards.already_scheduled* so a later *fetched* item sharing the
    same ``plex_rating_key`` is treated as already-scheduled, as the
    connection's uncommitted view would.
    """
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
    # no lock). See ScanEngine.sync_library for rationale.
    fetched = engine._fetcher.fetch_library_items(library_id)
    # An item whose watch history could not be fetched is still present in
    # Plex — record its key as seen so orphan removal never prunes it on a
    # scan that merely failed to fetch its history.
    if seen_keys is not None:
        seen_keys.update(fetched.skipped_keys)

    def _evaluate(
        f: PlexItemFetch,
        added_at: datetime,
        watch_history: Sequence[Mapping[str, object]],
    ) -> str | None:
        return evaluate_item(
            added_at=added_at,
            watch_history=watch_history,
            min_age_days=engine._min_age_days,
            inactivity_days=engine._inactivity_days,
        )

    scan_items(
        engine,
        fetched.items,
        media_type_fn=lambda f: "movie",
        evaluate_fn=_evaluate,
        item_label="Movie",
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
    # Phase 1: network fetch (see ScanEngine.sync_library).
    fetched = engine._fetcher.fetch_library_items(library_id)
    conn = engine._conn
    # A season whose watch history could not be fetched is still present in
    # Plex — record its key as seen so orphan removal never prunes it on a
    # scan that merely failed to fetch its history.
    if seen_keys is not None:
        seen_keys.update(fetched.skipped_keys)

    # Batching for the show-kept guard: a per-season repository.is_show_kept()
    # would issue a ``kept_shows`` SELECT (and a DELETE on the not-kept path)
    # for every season — O(seasons) round trips against a one-row-per-show
    # table. Instead: sweep expired snoozes once up front, then load the live
    # keep set in batched IN-queries so the per-season check is a pure set
    # membership test. ``kept_shows.show_rating_key`` is UNIQUE, so the set
    # answer equals a per-season LIMIT-1 read (an expired snooze is never
    # "kept", so removing it eagerly changes no decision).
    now = now_iso()
    repository.cleanup_expired_show_snoozes(conn, now)
    show_keys = [
        key
        for key in {f.item.get("show_rating_key") for f in fetched.items}
        if isinstance(key, str)
    ]
    kept_show_keys = repository.fetch_kept_show_keys(conn, show_keys, now)

    def _evaluate(
        f: PlexItemFetch,
        added_at: datetime,
        watch_history: Sequence[Mapping[str, object]],
    ) -> str | None:
        raw_key = f.item.get("show_rating_key")
        show_key = raw_key if isinstance(raw_key, str) else None
        if show_key is not None and show_key in kept_show_keys:
            return None  # show is protected; skip all its seasons
        return evaluate_item(
            added_at=added_at,
            watch_history=watch_history,
            min_age_days=engine._min_age_days,
            inactivity_days=engine._inactivity_days,
        )

    scan_items(
        engine,
        fetched.items,
        media_type_fn=lambda f: f.media_type,
        evaluate_fn=_evaluate,
        item_label="TV",
        summary=summary,
        seen_keys=seen_keys,
    )
