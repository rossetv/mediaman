"""Completion detection and recent-downloads persistence.

When an item disappears from the combined download queue between two
polls, we treat it as completed and record it in ``recent_downloads``
(subject to Radarr/Sonarr verification — a vanished item with no file
is probably a failed grab, not a finished one).

Kept separate from the queue builder so the pure completion logic
(``detect_completed``) can be unit-tested without any DB or HTTP
dependencies, and so the scheduler can import ``cleanup_recent_downloads``
without dragging in the whole queue module.

Package layout:

* ``_types`` — shared ``CompletedItem`` / ``RecentDownloadItem`` TypedDicts.
* ``_verification`` — ``_ArrLibraryIndex``, ``_check_item_verified``,
  ``_batch_insert_completions``, ``record_verified_completions``.
* ``_sync`` — ``detect_completed``, ``carry_queued_since``,
  ``cleanup_recent_downloads``, ``_PosterLookup``, ``_sync_recent_row``,
  ``fetch_and_sync_recent_downloads``.
"""

from __future__ import annotations

from mediaman.services.arr.completion._sync import (
    _PosterLookup,
    _sync_recent_row,
    carry_queued_since,
    cleanup_recent_downloads,
    detect_completed,
    fetch_and_sync_recent_downloads,
)
from mediaman.services.arr.completion._types import CompletedItem, RecentDownloadItem
from mediaman.services.arr.completion._verification import (
    _ArrLibraryIndex,
    _batch_insert_completions,
    _check_item_verified,
    _reset_unchecked,
    record_verified_completions,
)

__all__ = [
    "CompletedItem",
    "RecentDownloadItem",
    "_ArrLibraryIndex",
    "_PosterLookup",
    "_batch_insert_completions",
    "_check_item_verified",
    "_reset_unchecked",
    "_sync_recent_row",
    "carry_queued_since",
    "cleanup_recent_downloads",
    "detect_completed",
    "fetch_and_sync_recent_downloads",
    "record_verified_completions",
]
