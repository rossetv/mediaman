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
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from mediaman.scanner import repository
from mediaman.scanner.fetch import _PlexItemFetch
from mediaman.scanner.phases.evaluate import evaluate_movie, evaluate_season
from mediaman.scanner.phases.upsert import schedule_deletion as _phase_schedule_deletion
from mediaman.scanner.phases.upsert import upsert_item as _phase_upsert_item

if TYPE_CHECKING:
    from mediaman.scanner.engine import ScanEngine

logger = logging.getLogger(__name__)

__all__ = ["scan_items", "scan_movie_library", "scan_tv_library"]


def scan_items(
    engine: ScanEngine,
    fetched: list[_PlexItemFetch],
    media_type_fn: Callable[[_PlexItemFetch], str],
    evaluate_fn: Callable[[_PlexItemFetch, datetime, list[dict[str, object]]], str | None],
    item_label: str,
    library_id: str,
    summary: dict[str, int],
    seen_keys: set[str] | None = None,
) -> None:
    """Shared iteration skeleton for movie and TV scan passes.

    Iterates *fetched* items, upserts each one, applies the common
    protection/schedule guards, then delegates per-item evaluation to
    *evaluate_fn*. The two callers differ only in ``media_type_fn``
    (which selects the media type string from the fetch record) and
    ``evaluate_fn`` (which calls the appropriate evaluator and applies
    any domain-specific pre-skip logic such as the TV show-kept
    check).

    Args:
        engine: The owning :class:`ScanEngine` — provides DB connection,
            Arr-date cache, secret key, dry-run flag, and the
            ``_resolve_added_at`` helper.
        fetched: Pre-fetched items from the fetch phase.
        media_type_fn: Callable that returns the media_type string for
            a :class:`_PlexItemFetch` record.
        evaluate_fn: Callable ``(fetch, added_at, watch_history) ->
            decision`` where decision is ``"schedule_deletion"`` or any
            other value meaning "skip". May return ``None`` to signal
            an early skip (e.g. show-kept check failed before
            evaluation).
        item_label: Human-readable label used in exception log messages
            (e.g. ``"Movie"`` or ``"TV"``).
        library_id: Plex section ID — reserved for future use; the
            current body does not consume it, but keeping it in the
            signature mirrors the per-library shape of the callers and
            avoids churn in the rest of the call graph.
        summary: Mutable summary counter dict (``scanned``,
            ``scheduled``, ``skipped``, ``errors``).
        seen_keys: If provided, the item's Plex rating key is added so
            orphan detection can exclude it later.
    """
    del library_id  # reserved; see docstring
    conn = engine._conn
    for f in fetched:
        summary["scanned"] += 1
        item = f.item
        watch_history = f.watch_history
        try:
            media_id = item["plex_rating_key"]
            if seen_keys is not None:
                seen_keys.add(media_id)
            _phase_upsert_item(conn, f, engine._arr_cache, media_type_fn(f))
            repository.update_last_watched(conn, media_id, watch_history)

            if repository.is_protected(conn, media_id):
                summary["skipped"] += 1
                continue

            if repository.is_already_scheduled(conn, media_id):
                summary["skipped"] += 1
                continue

            added_at = engine._resolve_added_at(item)
            decision = evaluate_fn(f, added_at, watch_history)

            if decision is None:
                # evaluate_fn signalled an early skip (e.g. show-kept).
                summary["skipped"] += 1
                continue

            if decision == "schedule_deletion":
                if engine._dry_run:
                    # Dry-run preview: count what *would* be scheduled
                    # but write nothing. Both ``scheduled_actions`` and
                    # the audit_log row inside ``schedule_deletion``
                    # are skipped.
                    summary["scheduled"] += 1
                else:
                    is_reentry = repository.has_expired_snooze(conn, media_id)
                    _phase_schedule_deletion(
                        conn,
                        media_id=media_id,
                        is_reentry=is_reentry,
                        grace_days=engine._grace_days,
                        secret_key=engine._secret_key,
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
        f: _PlexItemFetch,
        added_at: datetime,
        watch_history: list[dict[str, object]],
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
        f: _PlexItemFetch,
        added_at: datetime,
        watch_history: list[dict[str, object]],
    ) -> str | None:
        season = f.item
        if repository.is_show_kept(conn, season.get("show_rating_key")):
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
