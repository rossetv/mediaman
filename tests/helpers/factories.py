"""Test data factories."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock


def make_media_item(
    *,
    id="12345",
    title="Test Movie",
    media_type="movie",
    show_title=None,
    season_number=None,
    plex_library_id=1,
    plex_rating_key="12345",
    sonarr_id=None,
    radarr_id=None,
    added_at=None,
    file_path="/media/movies/Test Movie (2024)",
    file_size_bytes=10_000_000_000,
    poster_path="/library/metadata/12345/thumb/1234",
):
    """Create a media item dict for testing."""
    if added_at is None:
        added_at = datetime.now(UTC) - timedelta(days=60)
    return {
        "id": id,
        "title": title,
        "media_type": media_type,
        "show_title": show_title,
        "season_number": season_number,
        "plex_library_id": plex_library_id,
        "plex_rating_key": plex_rating_key,
        "sonarr_id": sonarr_id,
        "radarr_id": radarr_id,
        "added_at": added_at.isoformat(),
        "file_path": file_path,
        "file_size_bytes": file_size_bytes,
        "poster_path": poster_path,
    }


def make_scheduled_action(
    *,
    media_item_id="12345",
    action="scheduled_deletion",
    scheduled_at=None,
    execute_at=None,
    token="test-token-abc",
    token_used=False,
    notified=False,
    is_reentry=False,
):
    """Create a scheduled action dict for testing."""
    now = datetime.now(UTC)
    if scheduled_at is None:
        scheduled_at = now
    if execute_at is None:
        execute_at = now + timedelta(days=14)
    return {
        "media_item_id": media_item_id,
        "action": action,
        "scheduled_at": scheduled_at.isoformat(),
        "execute_at": execute_at.isoformat(),
        "token": token,
        "token_used": token_used,
        "notified": notified,
        "is_reentry": is_reentry,
    }


def make_plex_episode(
    *,
    title="Episode 1",
    added_at=None,
    file_path="/data/tv/Show/Season 1/ep01.mkv",
    file_size_bytes=2_000_000_000,
    history=None,
):
    """Return a MagicMock shaped like a plexapi Episode object.

    Provides sensible defaults for the attributes that PlexClient reads
    when building a season record.  Pass keyword arguments to override
    specific fields for a given test.
    """
    if added_at is None:
        added_at = datetime(2026, 1, 10, tzinfo=UTC)
    ep = MagicMock()
    ep.title = title
    ep.addedAt = added_at
    part = MagicMock()
    part.file = file_path
    part.size = file_size_bytes
    media = MagicMock()
    media.parts = [part]
    ep.media = [media]
    ep.history.return_value = [] if history is None else history
    return ep


def make_plex_show(
    *,
    rating_key=100,
    title="Test Show",
    thumb="/library/metadata/100/thumb/1",
    seasons=None,
):
    """Return a MagicMock shaped like a plexapi Show object.

    ``seasons`` should be a list of season MagicMocks (e.g. built with
    ``make_plex_season``).  An empty list is the default so callers only
    need to supply the seasons relevant to their test.
    """
    show = MagicMock()
    show.ratingKey = rating_key
    show.title = title
    show.thumb = thumb
    show.seasons.return_value = [] if seasons is None else seasons
    return show


def make_plex_season(
    *,
    index=1,
    rating_key=200,
    added_at=None,
    episodes=None,
):
    """Return a MagicMock shaped like a plexapi Season object.

    ``episodes`` should be a list of episode MagicMocks (e.g. built with
    ``make_plex_episode``).
    """
    season = MagicMock()
    season.index = index
    season.ratingKey = rating_key
    season.addedAt = added_at if added_at is not None else datetime(2026, 1, 15, tzinfo=UTC)
    season.episodes.return_value = [] if episodes is None else episodes
    return season
