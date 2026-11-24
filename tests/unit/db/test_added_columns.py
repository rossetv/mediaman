"""init_db adds the later columns to a database made before them."""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.db import init_db
from mediaman.services.downloads._notification_claims import _claim_pending_notifications
from mediaman.web.repository.delete_intents import reconcile_pending_delete_intents

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

    assert {"tmdb_id", "tvdb_id", "tracked_files", "year"} <= _columns(conn)
    row = conn.execute("SELECT * FROM media_items").fetchall()
    assert len(row) == 1
    assert (row[0]["title"], row[0]["file_size_bytes"]) == ("T", 5)
    assert row[0]["tmdb_id"] is None and row[0]["tracked_files"] is None
    assert conn.execute("SELECT count(*) FROM deleted_media").fetchone()[0] == 0
    conn.close()


def test_fresh_database_has_columns(tmp_path):
    conn = init_db(str(tmp_path / "new.db"))
    assert {"tmdb_id", "tvdb_id", "tracked_files", "year"} <= _columns(conn)
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

    row = conn.execute("SELECT username FROM download_notifications").fetchone()
    assert row["username"] is None
    claimed = _claim_pending_notifications(conn)
    assert [(r.tvdb_id, r.season_number) for r in claimed] == [(9, None)]
    conn.close()


_OLD_DELETE_INTENTS = """
CREATE TABLE delete_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT
);
INSERT INTO delete_intents (media_item_id, target_kind, target_id, started_at)
VALUES ('m1', 'radarr', '1', '2020');
"""


def test_an_open_delete_intent_of_an_old_database_can_still_be_closed(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(_OLD_DELETE_INTENTS)
    old.close()

    init_db(path).close()
    conn = init_db(path)

    assert reconcile_pending_delete_intents(conn) == 1
    conn.close()


def test_an_old_database_records_when_a_search_was_last_seen_and_keeps_its_start(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE arr_searching_since (key TEXT PRIMARY KEY, since TEXT NOT NULL);"
        "INSERT INTO arr_searching_since VALUES ('radarr:1', '2020');"
    )
    old.close()

    init_db(path).close()
    conn = init_db(path)

    row = conn.execute("SELECT since, last_seen FROM arr_searching_since").fetchone()
    assert tuple(row) == ("2020", None)
    conn.close()


_OLD_WATCH_TABLES = """
CREATE TABLE watch_progress (
    username TEXT NOT NULL,
    rating_key TEXT NOT NULL,
    media_item_id TEXT,
    show_rating_key TEXT,
    position_s REAL NOT NULL DEFAULT 0,
    duration_s REAL NOT NULL DEFAULT 0,
    watched INTEGER NOT NULL DEFAULT 0,
    credited_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (username, rating_key)
);
INSERT INTO watch_progress (username, rating_key, position_s, updated_at)
VALUES ('amy', '7', 12.5, '2020');
CREATE TABLE suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO suggestions (title, media_type, created_at) VALUES ('T', 'movie', '2020');
"""


def test_an_old_database_gains_the_mark_columns_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(_OLD_WATCH_TABLES)
    old.close()

    init_db(path).close()
    conn = init_db(path)

    progress = conn.execute("SELECT position_s, seen_at FROM watch_progress").fetchone()
    assert tuple(progress) == (12.5, None)
    assert conn.execute("SELECT backdrop_url FROM suggestions").fetchone()[0] is None
    assert {"episode_count", "episode_keys"} <= _columns(conn)
    conn.close()


def test_a_dismissal_goes_with_its_user(tmp_path):
    conn = init_db(str(tmp_path / "new.db"))
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at) VALUES ('amy','h','2020')"
    )
    conn.execute("INSERT INTO watch_dismissed VALUES ('amy', 'show:1', '2020')")
    conn.commit()

    conn.execute("DELETE FROM admin_users WHERE username='amy'")

    assert conn.execute("SELECT count(*) FROM watch_dismissed").fetchone()[0] == 0
    conn.close()


def test_a_persons_picks_and_plex_plays_go_with_the_person_alone(tmp_path):
    conn = init_db(str(tmp_path / "new.db"))
    for number, name in enumerate(("amy", "bob"), start=1):
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?,'h','2020')",
            (name,),
        )
        conn.execute(
            "INSERT INTO user_picks (username, title, media_type, batch_id, created_at)"
            " VALUES (?, 'T', 'movie', '2026-01-01', '2026')",
            (name,),
        )
        conn.execute(
            "INSERT INTO user_pick_runs (username, requested_by, started_at)"
            " VALUES (?, 'scan', '2026')",
            (name,),
        )
        conn.execute(
            "INSERT INTO plex_links (username, plex_account_id, server_account_id, plex_name,"
            " server_id, linked_at) VALUES (?, ?, ?, 'P', 's', '2026')",
            (name, number, number),
        )
        conn.execute(
            "INSERT INTO plex_plays (username, history_key, media_type, title, viewed_at)"
            " VALUES (?, '/h/1', 'movie', 'T', '2026')",
            (name,),
        )
    conn.commit()

    conn.execute("DELETE FROM plex_links WHERE username='bob'")
    assert [r[0] for r in conn.execute("SELECT username FROM plex_plays")] == ["amy"]
    conn.execute("DELETE FROM admin_users WHERE username='amy'")
    for table in ("user_picks", "user_pick_runs"):
        assert [r[0] for r in conn.execute(f"SELECT username FROM {table}")] == ["bob"]
    assert conn.execute("SELECT count(*) FROM plex_plays").fetchone()[0] == 0
    conn.close()


def test_one_plex_account_belongs_to_one_person(tmp_path):
    conn = init_db(str(tmp_path / "new.db"))
    for name in ("amy", "bob"):
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?,'h','2020')",
            (name,),
        )
    link = (
        "INSERT INTO plex_links (username, plex_account_id, server_account_id, plex_name,"
        " server_id, linked_at) VALUES (?, ?, ?, 'P', 's', '2026')"
    )
    conn.execute(link, ("amy", 7, 7))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(link, ("bob", 7, 8))
    conn.close()


def test_plays_made_before_they_were_kept_apart_are_taken_over(tmp_path):
    path = str(tmp_path / "old.db")
    conn = init_db(path)
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at) VALUES ('amy','h','2020')"
    )
    for item, kind, title, show in (
        ("m", "movie", "Film", None),
        ("s", "tv_season", "Show S1", "Show"),
        ("u", "movie", "Unplayed", None),
    ):
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, show_title, plex_library_id,"
            " plex_rating_key, added_at, file_path, file_size_bytes, tmdb_id)"
            " VALUES (?, ?, ?, ?, 1, ?, '2020', '/m', 1, 5)",
            (item, title, kind, show, item),
        )
    conn.executemany(
        "INSERT INTO watch_progress (username, rating_key, media_item_id, show_rating_key,"
        " credited_at, updated_at) VALUES ('amy', ?, ?, ?, ?, '2024')",
        [
            ("m", "m", None, "2023"),
            ("e1", "s", "k", "2021"),
            ("e2", "s", "k", "2022"),
            ("u", "u", None, None),
        ],
    )
    conn.execute("DROP TABLE played_titles")
    conn.commit()
    conn.close()

    init_db(path).close()
    conn = init_db(path)

    rows = conn.execute("SELECT * FROM played_titles ORDER BY title").fetchall()
    assert [tuple(r) for r in rows] == [
        ("amy", "movie", "Film", 5, "2023"),
        ("amy", "tv", "Show", 5, "2022"),
    ]
    conn.close()
