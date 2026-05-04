"""Evaluate phase — decide whether a media item is eligible for deletion.

Wraps the per-type evaluators (:func:`evaluate_movie`,
:func:`evaluate_season`) in thin adapters that accept a
:class:`~mediaman.scanner.fetch._PlexItemFetch` record and return a
decision string:

* ``"schedule_deletion"`` — the item is eligible; the caller should queue it.
* Any other string (or ``None``) — the item is ineligible or should be
  skipped for a domain-specific reason (e.g. show-level keep rule).

The engine remains the authoritative orchestrator; this module only
separates the *per-item eligibility logic* from the *scan loop* so each
can be tested and reasoned about in isolation.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from mediaman.scanner import repository
from mediaman.scanner.fetch import _PlexItemFetch
from mediaman.scanner.movies import evaluate_movie
from mediaman.scanner.tv import evaluate_season


def evaluate_movie_item(
    fetch: _PlexItemFetch,
    added_at: datetime,
    watch_history: list[dict[str, Any]],
    *,
    min_age_days: int,
    inactivity_days: int,
) -> str | None:
    """Evaluate a movie item for deletion eligibility.

    Args:
        fetch: The fetched Plex record for this item.
        added_at: Best-available datetime when the file landed on disk
            (resolved by the engine from the Arr cache or Plex metadata).
        watch_history: List of watch-event dicts for this item.
        min_age_days: Minimum days since ``added_at`` before eligibility
            is assessed.
        inactivity_days: Days without a watch event that triggers deletion.

    Returns:
        ``"schedule_deletion"`` if the item qualifies, otherwise ``None``.
    """
    return evaluate_movie(
        added_at=added_at,
        watch_history=watch_history,
        min_age_days=min_age_days,
        inactivity_days=inactivity_days,
    )


def evaluate_season_item(
    fetch: _PlexItemFetch,
    added_at: datetime,
    watch_history: list[dict[str, Any]],
    *,
    conn: sqlite3.Connection,
    min_age_days: int,
    inactivity_days: int,
) -> str | None:
    """Evaluate a TV season for deletion eligibility.

    Performs the show-level keep check before delegating to the season
    evaluator.  Returns ``None`` (early skip) if the parent show is
    protected via ``kept_shows``.

    Args:
        fetch: The fetched Plex record for this season.
        added_at: Best-available ``added_at`` for this season.
        watch_history: Watch-event dicts for this season.
        conn: Open SQLite connection (read-only; only used for the show-kept
            check).
        min_age_days: Minimum days before eligibility is assessed.
        inactivity_days: Days of inactivity that trigger deletion.

    Returns:
        ``"schedule_deletion"``, ``None`` (skip — show is kept), or any
        other non-schedule string from :func:`evaluate_season`.
    """
    season = fetch.item
    if repository.is_show_kept(conn, season.get("show_rating_key")):
        return None  # show is protected; skip all its seasons

    return evaluate_season(
        added_at=added_at,
        episode_count=season.get("episode_count", 0),
        watch_history=watch_history,
        has_future_episodes=False,
        min_age_days=min_age_days,
        inactivity_days=inactivity_days,
    )
