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

* :mod:`._types` — shared :class:`CompletedItem` / :class:`RecentDownloadItem` TypedDicts.
* :mod:`._verification` — :class:`_ArrLibraryIndex`, :func:`_check_item_verified`,
  :func:`_batch_insert_completions`, :func:`record_verified_completions`.
* :mod:`._sync` — :func:`detect_completed`, :func:`cleanup_recent_downloads`,
  :class:`_PosterLookup`, :func:`_sync_recent_row`,
  :func:`fetch_and_sync_recent_downloads`.
"""

from __future__ import annotations

from mediaman.services.arr.completion._sync import (
    _PosterLookup,
    _sync_recent_row,
    cleanup_recent_downloads,
    detect_completed,
    fetch_and_sync_recent_downloads,
)
from mediaman.services.arr.completion._types import CompletedItem, RecentDownloadItem
from mediaman.services.arr.completion._verification import (
    _ArrLibraryIndex,
    _batch_insert_completions,
    _check_item_verified,
    record_verified_completions,
)

__all__ = [
    "CompletedItem",
    "RecentDownloadItem",
    "_ArrLibraryIndex",
    "_PosterLookup",
    "_batch_insert_completions",
    "_check_item_verified",
    "_sync_recent_row",
    "cleanup_recent_downloads",
    "detect_completed",
    "fetch_and_sync_recent_downloads",
    "record_verified_completions",
]
