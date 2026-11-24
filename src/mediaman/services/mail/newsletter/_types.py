"""TypedDict shapes shared across the newsletter submodules."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class ScheduledNewsletterItem(TypedDict):
    """A scheduled-deletion card. _action_id is an internal handle for marking rows
    notified=1 and must not reach the email HTML.
    """

    title: str
    media_type: str
    type_label: str
    poster_url: str
    file_size_bytes: int
    added_days_ago: int | None
    days_left: int
    last_watched_info: str | None
    keep_url: str
    is_reentry: bool
    _action_id: int


class DeletedNewsletterItem(TypedDict):
    """A recently-deleted card; tmdb_id is None when the deletion recorded none."""

    title: str
    poster_url: str
    deleted_date: str
    file_size_bytes: int
    media_type: str
    tmdb_id: int | None
    audit_id: NotRequired[int]
    season_number: NotRequired[int | None]
    # Added by the per-recipient render loop
    redownload_url: NotRequired[str]


class NewsletterRecItem(TypedDict):
    """A recommendation card; download_state is injected later by
    _annotate_rec_download_states. id is None on a person's own pick, which no link may name.
    """

    id: int | None
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
    total_bytes: int
    used_bytes: int
    free_bytes: int
    by_type: dict[str, int]
