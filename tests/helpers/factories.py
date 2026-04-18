"""Test data factories."""

from datetime import datetime, timedelta, timezone


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
        added_at = datetime.now(timezone.utc) - timedelta(days=60)
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
    now = datetime.now(timezone.utc)
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
