"""Test data factories.

The dict factories (``make_*``) build minimal-but-valid in-memory shapes
the test code can poke at; the ``insert_*`` companions take a connection
and persist the row, returning the assigned id (or the supplied id where
the caller pre-chose it). The two halves share defaults so a test that
needs a row inserted *and* available as a dict can do::

    fields = make_media_item(title="Foo")
    media_id = insert_media_item(conn, **fields)
"""

import sqlite3
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
    show_rating_key=None,
    added_at=None,
    file_path="/media/movies/Test Movie (2024)",
    file_size_bytes=10_000_000_000,
    poster_path="/library/metadata/12345/thumb/1234",
    last_watched_at=None,
    last_scanned_at=None,
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
        "show_rating_key": show_rating_key,
        "added_at": added_at.isoformat(),
        "file_path": file_path,
        "file_size_bytes": file_size_bytes,
        "poster_path": poster_path,
        "last_watched_at": last_watched_at,
        "last_scanned_at": last_scanned_at,
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


def insert_media_item(conn: sqlite3.Connection, **fields) -> str:
    """Persist a ``media_items`` row using the dict shape from :func:`make_media_item`.

    Returns the row's ``id``. Any field not supplied falls back to the
    factory's default — pass overrides for whatever the test cares about.
    Datetime values in *added_at*, *last_watched_at*, *last_scanned_at*
    are coerced to ISO strings (SQLite 3.12 deprecated the implicit
    adapter, and the schema stores TEXT anyway).
    """
    row = {**make_media_item(), **fields}
    for key in ("added_at", "last_watched_at", "last_scanned_at"):
        value = row.get(key)
        if isinstance(value, datetime):
            row[key] = value.isoformat()
    conn.execute(
        "INSERT INTO media_items ("
        " id, title, media_type, show_title, season_number, plex_library_id,"
        " plex_rating_key, sonarr_id, radarr_id, show_rating_key, added_at,"
        " file_path, file_size_bytes, poster_path, last_watched_at, last_scanned_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            row["title"],
            row["media_type"],
            row["show_title"],
            row["season_number"],
            row["plex_library_id"],
            row["plex_rating_key"],
            row["sonarr_id"],
            row["radarr_id"],
            row["show_rating_key"],
            row["added_at"],
            row["file_path"],
            row["file_size_bytes"],
            row["poster_path"],
            row["last_watched_at"],
            row["last_scanned_at"],
        ),
    )
    conn.commit()
    return row["id"]


def insert_scheduled_action(conn: sqlite3.Connection, **fields) -> int:
    """Persist a ``scheduled_actions`` row; return the assigned id.

    All columns except *media_item_id* have sane defaults.  Pass
    *delete_status* to control the two-phase-delete state column
    (defaults to ``"pending"``).  Pass *scheduled_at* to override the
    timestamp of when the action was created.
    """
    from mediaman.web.auth._token_hashing import hash_token

    defaults = {**make_scheduled_action(), "delete_status": "pending"}
    row = {**defaults, **fields}
    cursor = conn.execute(
        "INSERT INTO scheduled_actions ("
        " media_item_id, action, scheduled_at, execute_at, token, token_hash,"
        " token_used, notified, is_reentry, delete_status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["media_item_id"],
            row["action"],
            row["scheduled_at"],
            row["execute_at"],
            row["token"],
            hash_token(row["token"]),
            int(row["token_used"]),
            int(row["notified"]),
            int(row["is_reentry"]),
            row["delete_status"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_settings(conn: sqlite3.Connection, **fields) -> None:
    """Persist one or more rows to the ``settings`` table.

    Each keyword argument is treated as a separate row: the key is the
    settings key and the value is the plain-text value.  Pass
    ``encrypted=1`` (as a positional extra) to mark every row as
    encrypted, or wrap individual calls for mixed encryption.

    Example::

        insert_settings(conn, plex_url="http://localhost:32400", plex_token="abc")

    To control ``encrypted`` or ``updated_at`` per-row, call the function
    once per row with explicit kwargs::

        insert_settings(conn, openai_api_key="sk-...", encrypted=1,
                        updated_at="2026-01-01")
    """
    encrypted = int(fields.pop("encrypted", 0))
    updated_at = fields.pop("updated_at", None) or datetime.now(UTC).isoformat()
    for key, value in fields.items():
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, ?)",
            (key, value, encrypted, updated_at),
        )
    conn.commit()


def insert_audit_log(conn: sqlite3.Connection, **fields) -> int:
    """Persist an ``audit_log`` row; return the assigned id.

    Required override: ``media_item_id`` and ``action``.  All other
    fields default to sensible test values.
    """
    row: dict = {
        "media_item_id": "12345",
        "action": "deleted",
        "detail": None,
        "space_reclaimed_bytes": None,
        "created_at": datetime.now(UTC).isoformat(),
        "actor": None,
        **fields,
    }
    if isinstance(row.get("created_at"), datetime):
        row["created_at"] = row["created_at"].isoformat()
    cursor = conn.execute(
        "INSERT INTO audit_log"
        " (media_item_id, action, detail, space_reclaimed_bytes, created_at, actor)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            row["media_item_id"],
            row["action"],
            row["detail"],
            row["space_reclaimed_bytes"],
            row["created_at"],
            row["actor"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_kept_show(conn: sqlite3.Connection, **fields) -> int:
    """Persist a ``kept_shows`` row; return the assigned id.

    Defaults to a sensible test show.  Override any column via kwargs.
    """
    row: dict = {
        "show_rating_key": "rk100",
        "show_title": "Test Show",
        "action": "kept_show",
        "execute_at": None,
        "snooze_duration": None,
        "created_at": datetime.now(UTC).isoformat(),
        **fields,
    }
    if isinstance(row.get("created_at"), datetime):
        row["created_at"] = row["created_at"].isoformat()
    cursor = conn.execute(
        "INSERT INTO kept_shows"
        " (show_rating_key, show_title, action, execute_at, snooze_duration, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            row["show_rating_key"],
            row["show_title"],
            row["action"],
            row["execute_at"],
            row["snooze_duration"],
            row["created_at"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_subscriber(conn: sqlite3.Connection, **fields) -> int:
    """Persist a ``subscribers`` row; return the assigned id.

    Defaults to a single active subscriber.  Override any column via
    kwargs.
    """
    row: dict = {
        "email": "subscriber@example.com",
        "active": 1,
        "created_at": datetime.now(UTC).isoformat(),
        **fields,
    }
    if isinstance(row.get("created_at"), datetime):
        row["created_at"] = row["created_at"].isoformat()
    cursor = conn.execute(
        "INSERT INTO subscribers (email, active, created_at) VALUES (?, ?, ?)",
        (row["email"], int(row["active"]), row["created_at"]),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_suggestion(conn: sqlite3.Connection, **fields) -> int:
    """Persist a ``suggestions`` row; return the assigned id.

    Defaults to a minimal movie suggestion.  Override any column via
    kwargs, including the optional TMDB/IMDB/rating fields.
    """
    row: dict = {
        "title": "Test Suggestion",
        "year": None,
        "media_type": "movie",
        "category": "personal",
        "tmdb_id": None,
        "imdb_id": None,
        "description": None,
        "reason": None,
        "poster_url": None,
        "trailer_url": None,
        "rating": None,
        "rt_rating": None,
        "tagline": None,
        "runtime": None,
        "genres": None,
        "cast_json": None,
        "director": None,
        "trailer_key": None,
        "imdb_rating": None,
        "metascore": None,
        "batch_id": None,
        "downloaded_at": None,
        "created_at": datetime.now(UTC).isoformat(),
        **fields,
    }
    if isinstance(row.get("created_at"), datetime):
        row["created_at"] = row["created_at"].isoformat()
    cursor = conn.execute(
        "INSERT INTO suggestions ("
        " title, year, media_type, category, tmdb_id, imdb_id, description, reason,"
        " poster_url, trailer_url, rating, rt_rating, tagline, runtime, genres,"
        " cast_json, director, trailer_key, imdb_rating, metascore, batch_id,"
        " downloaded_at, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["title"],
            row["year"],
            row["media_type"],
            row["category"],
            row["tmdb_id"],
            row["imdb_id"],
            row["description"],
            row["reason"],
            row["poster_url"],
            row["trailer_url"],
            row["rating"],
            row["rt_rating"],
            row["tagline"],
            row["runtime"],
            row["genres"],
            row["cast_json"],
            row["director"],
            row["trailer_key"],
            row["imdb_rating"],
            row["metascore"],
            row["batch_id"],
            row["downloaded_at"],
            row["created_at"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_recent_download(conn: sqlite3.Connection, **fields) -> int:
    """Persist a ``recent_downloads`` row; return the assigned id.

    Defaults to a minimal completed movie download.
    """
    row: dict = {
        "dl_id": "nzbget-12345",
        "title": "Test Movie",
        "media_type": "movie",
        "poster_url": "",
        "completed_at": datetime.now(UTC).isoformat(),
        **fields,
    }
    if isinstance(row.get("completed_at"), datetime):
        row["completed_at"] = row["completed_at"].isoformat()
    cursor = conn.execute(
        "INSERT INTO recent_downloads (dl_id, title, media_type, poster_url, completed_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            row["dl_id"],
            row["title"],
            row["media_type"],
            row["poster_url"],
            row["completed_at"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_download_notification(conn: sqlite3.Connection, **fields) -> int:
    """Persist a ``download_notifications`` row; return the assigned id.

    Defaults to a pending (notified=0) movie notification.
    """
    row: dict = {
        "email": "user@example.com",
        "title": "Test Movie",
        "media_type": "movie",
        "tmdb_id": None,
        "service": "radarr",
        "notified": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "tvdb_id": None,
        "claimed_at": None,
        **fields,
    }
    if isinstance(row.get("created_at"), datetime):
        row["created_at"] = row["created_at"].isoformat()
    cursor = conn.execute(
        "INSERT INTO download_notifications"
        " (email, title, media_type, tmdb_id, service, notified, created_at, tvdb_id, claimed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["email"],
            row["title"],
            row["media_type"],
            row["tmdb_id"],
            row["service"],
            int(row["notified"]),
            row["created_at"],
            row["tvdb_id"],
            row["claimed_at"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_admin_user(conn: sqlite3.Connection, **fields) -> int:
    """Persist an ``admin_users`` row via :func:`~mediaman.web.auth.password_hash.create_user`.

    Returns the new row's id.  Defaults to username ``"admin"`` with
    password ``"password1234"`` so most tests need zero arguments.

    The ``enforce_policy`` flag is always ``False`` in test builds — the
    password policy checker runs against the live bcrypt stack and would
    add unacceptable latency to the test suite.
    """
    from mediaman.web.auth.password_hash import create_user

    username = fields.get("username", "admin")
    password = fields.get("password", "password1234")
    create_user(conn, username, password, enforce_policy=False)
    row = conn.execute(
        "SELECT id FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    return int(row["id"])


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
