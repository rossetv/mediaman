"""Domain types and decision helpers for ``scheduled_actions``.

Defines the two dataclasses shared across the scheduled-actions service
(``KeepDecision`` and ``VerifiedKeepAction``) and the pure-logic
``resolve_keep_decision`` function that maps a duration string to the
first dataclass.  Kept separate so the DB-lookup and mutation modules can
import the types without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from mediaman.core.scheduled_action_kinds import (
    ACTION_PROTECTED_FOREVER,
    ACTION_SNOOZED,
)


@dataclass(frozen=True, slots=True)
class KeepDecision:
    """Resolved outcome of a keep-duration choice.

    ``action`` is one of :data:`ACTION_PROTECTED_FOREVER` /
    :data:`ACTION_SNOOZED`.  ``execute_at`` is the ISO UTC deadline for
    a finite snooze, ``None`` for forever.  ``snooze_duration_days`` is
    the integer day count for a finite snooze, ``None`` for forever.
    """

    action: str
    execute_at: str | None
    snooze_duration_days: int | None


def resolve_keep_decision(duration: str, *, days: int | None, now: datetime) -> KeepDecision:
    """Resolve the keep-duration ladder once.

    Two route handlers previously duplicated this if/else chain:
    ``web/routes/library_api/__init__.py`` (``api_media_keep``) and
    ``web/routes/kept.py`` (``api_keep_show``).  ``duration`` must be a
    value already validated against :data:`VALID_KEEP_DURATIONS`;
    ``days`` is the integer day count from the same lookup
    (``VALID_KEEP_DURATIONS[duration]``) — ``None`` only when
    ``duration == "forever"``.  Passing them separately rather than
    importing :data:`VALID_KEEP_DURATIONS` here keeps Ring-2 services
    free of any Ring-3 ``web.models`` dependency.
    """
    if duration == "forever":
        return KeepDecision(ACTION_PROTECTED_FOREVER, None, None)
    assert days is not None, "non-forever durations must have a day count"
    execute_at = (now + timedelta(days=days)).isoformat()
    return KeepDecision(ACTION_SNOOZED, execute_at, days)


@dataclass(frozen=True, slots=True)
class VerifiedKeepAction:
    """A ``scheduled_actions`` row joined with its parent ``media_items`` row.

    Returned by :func:`~mediaman.services.scheduled_actions.lookup_verified_action` and
    :func:`~mediaman.services.scheduled_actions.find_active_keep_action_by_id_and_token`
    so the keep routes consume typed attributes rather than raw
    ``sqlite3.Row`` string keys (per the repository-returns-dataclasses
    standard).  The field order mirrors the shared SELECT column list: all
    ``scheduled_actions`` columns first, then the ``media_items`` display
    columns.  Nullable DB columns are typed ``... | None`` accordingly.
    """

    # scheduled_actions columns
    id: int
    media_item_id: str
    action: str
    scheduled_at: str
    execute_at: str | None
    token: str | None
    token_used: int
    snoozed_at: str | None
    snooze_duration: str | None
    notified: int
    is_reentry: int
    delete_status: str | None
    token_hash: str | None
    # media_items columns (joined for display)
    title: str
    media_type: str
    poster_path: str | None
    file_size_bytes: int
    plex_rating_key: str
    added_at: str
    show_title: str | None
    season_number: int | None
