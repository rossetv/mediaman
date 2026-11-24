"""init_db adds the later columns to a database made before them."""

from __future__ import annotations

import sqlite3

from mediaman.db import init_db
from mediaman.services.downloads._notification_claims import _claim_pending_notifications

_OLD_MEDIA_ITEMS = """
CREATE TABLE media_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    show_title TEXT,
    season_number INTEGER,
    plex_library_id INTEGER NOT NULL,
    plex_rating_key TEXT NOT NULL,
    sonarr_id INTEGER,
    radarr_id INTEGER,
    show_rating_key TEXT,
    added_at TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    poster_path TEXT,
    last_watched_at TEXT,
    last_scanned_at TEXT
);
"""


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(media_items)")}


def test_old_database_gains_columns_and_keeps_rows(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(_OLD_MEDIA_ITEMS)
    old.execute(
        "INSERT INTO media_items (id, title, media_type, plex_library_id, plex_rating_key,"
        " added_at, file_path, file_size_bytes) VALUES ('1','T','movie',1,'k','2020','/m/T',5)"
    )
    old.commit()
    old.close()

    init_db(path).close()
    conn = init_db(path)

    assert {"tmdb_id", "tvdb_id", "tracked_files"} <= _columns(conn)
    row = conn.execute("SELECT * FROM media_items").fetchall()
    assert len(row) == 1
    assert (row[0]["title"], row[0]["file_size_bytes"]) == ("T", 5)
    assert row[0]["tmdb_id"] is None and row[0]["tracked_files"] is None
    assert conn.execute("SELECT count(*) FROM deleted_media").fetchone()[0] == 0
    conn.close()


def test_fresh_database_has_columns(tmp_path):
    conn = init_db(str(tmp_path / "new.db"))
    assert {"tmdb_id", "tvdb_id", "tracked_files"} <= _columns(conn)
    conn.close()


_OLD_DOWNLOAD_NOTIFICATIONS = """
CREATE TABLE download_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER,
    service TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    tvdb_id INTEGER,
    claimed_at TEXT
);
INSERT INTO download_notifications (email, title, media_type, service, created_at, tvdb_id)
VALUES ('a@example.com', 'Show', 'tv', 'sonarr', '2020', 9);
"""


def test_a_pending_notification_of_an_old_database_can_still_be_claimed(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(_OLD_DOWNLOAD_NOTIFICATIONS)
    old.close()

    init_db(path).close()
    conn = init_db(path)

    claimed = _claim_pending_notifications(conn)
    assert [(r.tvdb_id, r.season_number) for r in claimed] == [(9, None)]
    conn.close()
