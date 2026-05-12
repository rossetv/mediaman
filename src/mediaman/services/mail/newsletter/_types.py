"""TypedDict shapes shared across the newsletter submodules."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class ScheduledNewsletterItem(TypedDict):
    """A scheduled-deletion card as rendered in the newsletter.

    Built by :func:`.schedule._load_scheduled_items`.  The ``_action_id``
    field is an internal handle used to mark rows as ``notified=1`` and
    must not be exposed in outbound email HTML.
    """

    title: str
    media_type: str
    type_label: str
    poster_url: str
    file_size_bytes: int
    added_days_ago: int | None
    last_watched_info: str | None
    keep_url: str
    is_reentry: bool
    _action_id: int


class DeletedNewsletterItem(TypedDict):
    """A recently-deleted card as rendered in the newsletter.

    Built by :func:`.summary._load_deleted_items`.  ``tmdb_id`` is absent
    for items that did not flow through the recommendation pipeline.
    """

    title: str
    poster_url: str
    deleted_date: str
    file_size_bytes: int
    media_type: str
    tmdb_id: int | None
    # Added by the per-recipient render loop
    redownload_url: NotRequired[str]


class NewsletterRecItem(TypedDict):
    """A recommendation card as rendered in the newsletter.

    Built by :func:`.summary._load_recommendations` from the ``suggestions``
    table; ``download_state`` is injected later by
    :func:`.enrich._annotate_rec_download_states`.
    """

    id: int
    title: str
    media_type: str
    category: str
    description: str | None
    reason: str | None
    poster_url: str | None
    tmdb_id: int | None
    rating: float | None
    rt_rating: str | None
    # Added by enrich stage
    download_state: NotRequired[str | None]
    # Added by per-recipient render loop
    download_url: NotRequired[str]


class StorageStats(TypedDict):
    """Disk-usage summary passed to the newsletter template.

    Built by :func:`.summary._load_storage_stats`.
    """

    total_bytes: int
    used_bytes: int
    free_bytes: int
    by_type: dict[str, int]
