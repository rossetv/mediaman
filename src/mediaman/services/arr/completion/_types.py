"""Shared TypedDicts for the completion package.

``CompletedItem`` and ``RecentDownloadItem`` are used by both
the verification module and the sync module — kept here to avoid a
circular import between the two.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class RecentDownloadItem(TypedDict):
    """A recent download row returned by ``fetch_and_sync_recent_downloads``.

    Matches the shape stored in ``recent_downloads`` and returned to callers
    such as ``build_downloads_response``.
    """

    id: str  # dl_id — the unique download identifier
    title: str
    media_type: str
    poster_url: str
    completed_at: str


class CompletedItem(TypedDict):
    dl_id: str
    title: str
    media_type: str
    poster_url: str
    #: Optional TMDB ID added by callers that enrich the list before passing
    #: it to ``record_verified_completions``.  Not set by
    #: ``detect_completed`` itself.
    tmdb_id: NotRequired[int | None]
    #: Labels (``S01E02``) of the episodes that were in the queue; absent when it named none.
    episodes: NotRequired[list[str]]
    #: When it was first seen in the queue, as ISO text; see ``carry_queued_since``.
    queued_since: NotRequired[str]
