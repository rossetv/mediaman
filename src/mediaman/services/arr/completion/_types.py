"""Shared TypedDicts for the completion package.

:class:`CompletedItem` and :class:`RecentDownloadItem` are used by both
the verification module and the sync module — kept here to avoid a
circular import between the two.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class RecentDownloadItem(TypedDict):
    """A recent download row returned by :func:`~completion.fetch_and_sync_recent_downloads`.

    Matches the shape stored in ``recent_downloads`` and returned to callers
    such as :func:`~mediaman.services.downloads.download_queue.build_downloads_response`.
    """

    id: str  # dl_id — the unique download identifier
    title: str
    media_type: str
    poster_url: str
    completed_at: str


class CompletedItem(TypedDict):
    """A download item that has disappeared from the queue (i.e. completed)."""

    dl_id: str
    title: str
    media_type: str
    poster_url: str
    #: Optional TMDB ID added by callers that enrich the list before passing
    #: it to :func:`~completion.record_verified_completions`.  Not set by
    #: :func:`~completion.detect_completed` itself.
    tmdb_id: NotRequired[int | None]
