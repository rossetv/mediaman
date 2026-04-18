"""Tests for the redesigned downloads API."""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.db import init_db


class TestRecentDownloadsTable:
    def test_recent_downloads_table_exists(self, db_path):
        """Migration v9 creates the recent_downloads table."""
        conn = init_db(str(db_path))
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "recent_downloads" in tables

    def test_recent_downloads_columns(self, db_path):
        """recent_downloads has the expected columns."""
        conn = init_db(str(db_path))
        cols = [
            r[1] for r in conn.execute("PRAGMA table_info(recent_downloads)").fetchall()
        ]
        assert "dl_id" in cols
        assert "title" in cols
        assert "media_type" in cols
        assert "poster_url" in cols
        assert "completed_at" in cols

    def test_recent_downloads_unique_dl_id(self, db_path):
        """dl_id has a UNIQUE constraint — duplicate inserts fail."""
        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, ?)",
            ("radarr:Dune", "Dune", "movie"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, ?)",
                ("radarr:Dune", "Dune", "movie"),
            )


from mediaman.services.download_format import _map_state, _build_item, _select_hero


class TestStateMapping:
    def test_searching_state(self):
        """Item in Arr queue with no NZBGet match → searching."""
        assert _map_state(nzbget_status=None, has_nzbget_match=False) == "searching"

    def test_downloading_state(self):
        """NZBGet status contains DOWNLOADING → downloading."""
        assert _map_state(nzbget_status="DOWNLOADING", has_nzbget_match=True) == "downloading"

    def test_almost_ready_unpacking(self):
        """NZBGet status contains UNPACKING → almost_ready."""
        assert _map_state(nzbget_status="UNPACKING", has_nzbget_match=True) == "almost_ready"

    def test_almost_ready_postprocessing(self):
        """NZBGet status contains PP_ → almost_ready."""
        assert _map_state(nzbget_status="PP_QUEUED", has_nzbget_match=True) == "almost_ready"

    def test_queued_state(self):
        """NZBGet status is QUEUED → searching (not yet actively downloading)."""
        assert _map_state(nzbget_status="QUEUED", has_nzbget_match=True) == "searching"

    def test_paused_state(self):
        """NZBGet status is PAUSED → downloading (still has progress)."""
        assert _map_state(nzbget_status="PAUSED", has_nzbget_match=True) == "downloading"


class TestBuildItem:
    def test_movie_item_shape(self):
        """A movie item has the expected fields."""
        item = _build_item(
            dl_id="radarr:Dune",
            title="Dune: Part Two",
            media_type="movie",
            poster_url="https://example.com/poster.jpg",
            state="downloading",
            progress=67,
            eta="~12 min remaining",
            size_done="4.2 GB",
            size_total="6.3 GB",
        )
        assert item["id"] == "radarr:Dune"
        assert item["title"] == "Dune: Part Two"
        assert item["media_type"] == "movie"
        assert item["state"] == "downloading"
        assert item["progress"] == 67
        assert item["episodes"] is None

    def test_series_item_has_episodes(self):
        """A series item includes the episodes list."""
        episodes = [
            {"label": "S03E01", "title": "Pilot", "state": "ready", "progress": 100},
            {"label": "S03E02", "title": "Two", "state": "downloading", "progress": 45},
        ]
        item = _build_item(
            dl_id="sonarr:Severance",
            title="Severance",
            media_type="series",
            poster_url="",
            state="downloading",
            progress=72,
            eta="~20 min remaining",
            size_done="3.4 GB",
            size_total="4.7 GB",
            episodes=episodes,
        )
        assert item["media_type"] == "series"
        assert len(item["episodes"]) == 2
        assert item["episodes"][0]["state"] == "ready"

    def test_upcoming_item_has_release_label(self):
        item = _build_item(
            dl_id="radarr:FutureFilm",
            title="Future Film",
            media_type="movie",
            poster_url="",
            state="upcoming",
            progress=0,
            eta="",
            size_done="",
            size_total="",
            release_label="Releases 14 Jun 2099",
        )
        assert item["state"] == "upcoming"
        assert item["release_label"] == "Releases 14 Jun 2099"

    def test_default_release_label_is_empty(self):
        item = _build_item(
            dl_id="radarr:Dune",
            title="Dune",
            media_type="movie",
            poster_url="",
            state="downloading",
            progress=50,
            eta="",
            size_done="",
            size_total="",
        )
        assert item["release_label"] == ""


class TestHeroSelection:
    def test_single_item_is_hero(self):
        """A single item in the queue becomes the hero."""
        items = [_build_item("r:A", "A", "movie", "", "downloading", 50, "", "", "")]
        hero, rest = _select_hero(items)
        assert hero["id"] == "r:A"
        assert rest == []

    def test_highest_progress_downloading_is_hero(self):
        """The actively downloading item with the highest progress wins."""
        items = [
            _build_item("r:A", "A", "movie", "", "searching", 0, "", "", ""),
            _build_item("r:B", "B", "movie", "", "downloading", 30, "", "", ""),
            _build_item("r:C", "C", "movie", "", "downloading", 80, "", "", ""),
        ]
        hero, rest = _select_hero(items)
        assert hero["id"] == "r:C"
        assert len(rest) == 2

    def test_no_downloading_picks_first(self):
        """When all items are searching, the first item is the hero."""
        items = [
            _build_item("r:A", "A", "movie", "", "searching", 0, "", "", ""),
            _build_item("r:B", "B", "movie", "", "searching", 0, "", "", ""),
        ]
        hero, rest = _select_hero(items)
        assert hero["id"] == "r:A"
        assert len(rest) == 1

    def test_empty_queue_returns_none(self):
        """Empty queue returns None hero."""
        hero, rest = _select_hero([])
        assert hero is None
        assert rest == []


from mediaman.services.arr_completion import _detect_completed
from mediaman.services.download_queue import _reset_previous_queue


class TestCompletionDetection:
    def setup_method(self):
        """Reset state between tests."""
        _reset_previous_queue()

    def test_item_disappearing_is_completed(self):
        """An item present previously but absent now is detected as completed."""
        previous = {"radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "media_type": "movie", "poster_url": ""}}
        current = {}
        completed = _detect_completed(previous, current)
        assert len(completed) == 1
        assert completed[0]["dl_id"] == "radarr:Dune"

    def test_no_change_means_no_completions(self):
        """Same items in both snapshots → nothing completed."""
        snapshot = {"radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "media_type": "movie", "poster_url": ""}}
        completed = _detect_completed(snapshot, snapshot)
        assert completed == []

    def test_new_item_is_not_completed(self):
        """An item appearing for the first time is not a completion."""
        previous = {}
        current = {"radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "media_type": "movie", "poster_url": ""}}
        completed = _detect_completed(previous, current)
        assert completed == []

    def test_reset_clears_previous(self):
        """_reset_previous_queue clears the in-memory snapshot."""
        _reset_previous_queue()  # Should not raise


from mediaman.db import init_db, set_connection
from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.web.routes.download import router as download_router

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch


def _make_download_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(download_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


class TestDownloadStatusAPI:
    def test_status_returns_new_shape_fields(self, db_path, secret_key):
        """GET /api/download/status returns the new item shape with state field."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234")
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_queue_item = {
            "movie": {
                "title": "Dune",
                "tmdbId": 123,
                "images": [{"coverType": "poster", "remoteUrl": "http://img/poster.jpg"}],
            },
            "size": 6000000000,
            "sizeleft": 2000000000,
            "status": "downloading",
            "trackedDownloadStatus": "ok",
            "timeleft": "00:12:00",
        }
        mock_client = MagicMock()
        mock_client.get_queue.return_value = [mock_queue_item]
        mock_client.get_movie_by_tmdb.return_value = {"hasFile": False, "tmdbId": 123}

        with patch("mediaman.web.routes.download._build_radarr", return_value=mock_client):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=123")

        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert data["state"] == "downloading"
        assert "progress" in data
        assert "eta" in data
        assert "episodes" in data

    def test_status_unknown_service(self, db_path, secret_key):
        """Missing service returns unknown state."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234")
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        resp = client.get("/api/download/status?service=&tmdb_id=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "unknown"

    def test_status_radarr_ready(self, db_path, secret_key):
        """Movie with hasFile=True returns state=ready with progress=100."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234")
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {
            "hasFile": True,
            "title": "Dune",
            "tmdbId": 42,
            "images": [],
        }

        with patch("mediaman.web.routes.download._build_radarr", return_value=mock_client):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ready"
        assert data["progress"] == 100

    def test_status_radarr_searching(self, db_path, secret_key):
        """Movie not in queue and no file → state=searching."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234")
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = None
        mock_client.get_queue.return_value = []

        with patch("mediaman.web.routes.download._build_radarr", return_value=mock_client):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=999")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "searching"

    def test_status_timeleft_formatting(self, db_path, secret_key):
        """timeleft HH:MM:SS is formatted as human-readable eta."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234")
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_queue_item = {
            "movie": {"title": "Test", "tmdbId": 77, "images": []},
            "size": 4000000000,
            "sizeleft": 1000000000,
            "status": "downloading",
            "trackedDownloadStatus": "downloading",
            "timeleft": "01:30:00",
        }
        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {"hasFile": False}
        mock_client.get_queue.return_value = [mock_queue_item]

        with patch("mediaman.web.routes.download._build_radarr", return_value=mock_client):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=77")

        assert resp.status_code == 200
        data = resp.json()
        assert "1 hr" in data["eta"]
        assert "remaining" in data["eta"]


class TestRecentDownloadsCleanup:
    def test_cleanup_removes_old_rows(self, db_path):
        """Rows older than 7 days are purged."""
        conn = init_db(str(db_path))
        # Insert a row dated 10 days ago
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type, completed_at) VALUES (?, ?, ?, datetime('now', '-10 days'))",
            ("radarr:Old", "Old Movie", "movie"),
        )
        # Insert a row from today
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, ?)",
            ("radarr:New", "New Movie", "movie"),
        )
        conn.commit()

        from mediaman.services.arr_completion import cleanup_recent_downloads
        cleanup_recent_downloads(conn)

        rows = conn.execute("SELECT dl_id FROM recent_downloads").fetchall()
        dl_ids = [r["dl_id"] for r in rows]
        assert "radarr:Old" not in dl_ids
        assert "radarr:New" in dl_ids


from mediaman.services.download_format import _classify_movie_upcoming, _classify_series_upcoming


class TestClassifyMovieUpcoming:
    def test_not_available_movie_is_upcoming(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "digitalRelease": "2099-06-14T00:00:00Z",
        }
        is_upcoming, label = _classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert label.startswith("Releases ")
        assert "2099" in label

    def test_available_movie_is_not_upcoming(self):
        movie = {"monitored": True, "hasFile": False, "isAvailable": True}
        is_upcoming, label = _classify_movie_upcoming(movie)
        assert is_upcoming is False
        assert label == ""

    def test_unmonitored_movie_is_not_upcoming(self):
        movie = {"monitored": False, "hasFile": False, "isAvailable": False}
        is_upcoming, label = _classify_movie_upcoming(movie)
        assert is_upcoming is False

    def test_already_has_file_is_not_upcoming(self):
        movie = {"monitored": True, "hasFile": True, "isAvailable": False}
        is_upcoming, label = _classify_movie_upcoming(movie)
        assert is_upcoming is False

    def test_upcoming_with_no_release_dates_has_fallback_label(self):
        movie = {"monitored": True, "hasFile": False, "isAvailable": False}
        is_upcoming, label = _classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert label == "Not yet released"

    def test_label_picks_earliest_future_date(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "digitalRelease": "2099-06-14T00:00:00Z",
            "physicalRelease": "2099-09-01T00:00:00Z",
            "inCinemas": "2099-03-15T00:00:00Z",
        }
        is_upcoming, label = _classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert "2099" in label
        assert "Mar" in label

    def test_label_ignores_past_dates(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "inCinemas": "1999-01-01T00:00:00Z",
            "digitalRelease": "2099-12-01T00:00:00Z",
        }
        is_upcoming, label = _classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert "2099" in label
        assert "Dec" in label

    def test_label_all_past_dates_falls_back(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "digitalRelease": "1999-01-01T00:00:00Z",
            "physicalRelease": "2000-01-01T00:00:00Z",
            "inCinemas": "2001-01-01T00:00:00Z",
        }
        is_upcoming, label = _classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert label == "Not yet released"


class TestClassifySeriesUpcoming:
    def test_upcoming_status_is_upcoming(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 0},
        }
        is_upcoming, label = _classify_series_upcoming(series, episodes=[])
        assert is_upcoming is True

    def test_continuing_with_aired_episodes_is_not_upcoming(self):
        series = {
            "monitored": True,
            "status": "continuing",
            "statistics": {"episodeFileCount": 0},
        }
        episodes = [{"airDateUtc": "2020-01-01T00:00:00Z"}]
        is_upcoming, label = _classify_series_upcoming(series, episodes=episodes)
        assert is_upcoming is False

    def test_unmonitored_is_not_upcoming(self):
        series = {"monitored": False, "status": "upcoming"}
        is_upcoming, label = _classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False

    def test_has_episode_files_is_not_upcoming(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 3},
        }
        is_upcoming, label = _classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False

    def test_all_future_episodes_with_continuing_status_is_upcoming(self):
        series = {
            "monitored": True,
            "status": "continuing",
            "statistics": {"episodeFileCount": 0},
        }
        episodes = [{"airDateUtc": "2099-12-01T00:00:00Z"}]
        is_upcoming, label = _classify_series_upcoming(series, episodes=episodes)
        assert is_upcoming is True
        assert "2099" in label
        assert label.startswith("Premieres ")

    def test_upcoming_label_with_no_air_dates_has_fallback(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 0},
        }
        is_upcoming, label = _classify_series_upcoming(series, episodes=[])
        assert is_upcoming is True
        assert label == "Not yet aired"

    def test_label_picks_earliest_future_airdate(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 0},
        }
        episodes = [
            {"airDateUtc": "2099-12-01T00:00:00Z"},
            {"airDateUtc": "2099-03-15T00:00:00Z"},
            {"airDateUtc": "2099-06-14T00:00:00Z"},
        ]
        is_upcoming, label = _classify_series_upcoming(series, episodes=episodes)
        assert is_upcoming is True
        assert "Mar" in label
        assert "2099" in label

    def test_ended_series_with_empty_episodes_is_not_upcoming(self):
        """An ended/continuing series with no episodes fetched is NOT classified as upcoming.

        Protects against misclassifying series whose episode metadata hasn't loaded yet
        or whose get_episodes() call failed.
        """
        series = {
            "monitored": True,
            "status": "ended",
            "statistics": {"episodeFileCount": 0},
        }
        is_upcoming, label = _classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False
        assert label == ""


from unittest.mock import MagicMock, patch
from mediaman.services.download_queue import _get_arr_queue


class TestGetArrQueueEnrichment:
    def _mock_conn_with_radarr_setting(self):
        conn = MagicMock()

        def fake_execute(sql, params=()):
            row = MagicMock()
            if params == ("radarr_url",):
                row.__getitem__.side_effect = lambda k: {
                    "value": "https://radarr.local",
                    "encrypted": 0,
                }[k]
                cursor = MagicMock()
                cursor.fetchone.return_value = row
                return cursor
            if params == ("radarr_api_key",):
                row.__getitem__.side_effect = lambda k: {
                    "value": "key123",
                    "encrypted": 0,
                }[k]
                cursor = MagicMock()
                cursor.fetchone.return_value = row
                return cursor
            cursor = MagicMock()
            cursor.fetchone.return_value = None
            return cursor

        conn.execute.side_effect = fake_execute
        return conn

    def _patched_arr_queue(self, conn, mock_client):
        """Run _get_arr_queue with load_config and RadarrClient patched."""
        from mediaman.config import Config

        fake_config = Config(secret_key="test-secret-key-for-unit-tests-only")
        with patch("mediaman.config.load_config", return_value=fake_config):
            with patch(
                "mediaman.services.radarr.RadarrClient", return_value=mock_client
            ):
                return _get_arr_queue(conn)

    def test_upcoming_movie_is_included_regardless_of_added_date(self):
        old_movie = {
            "id": 99,
            "title": "Old Upcoming",
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "added": "2020-01-01T00:00:00Z",
            "digitalRelease": "2099-06-14T00:00:00Z",
            "images": [],
        }
        mock_client = MagicMock()
        mock_client.get_queue.return_value = []
        mock_client.get_movies.return_value = [old_movie]

        conn = self._mock_conn_with_radarr_setting()
        items = self._patched_arr_queue(conn, mock_client)

        titles = [i["title"] for i in items]
        assert "Old Upcoming" in titles
        hit = next(i for i in items if i["title"] == "Old Upcoming")
        assert hit["is_upcoming"] is True
        assert "2099" in hit["release_label"]
        assert hit["arr_id"] == 99

    def test_stalled_released_movie_is_included_regardless_of_added_date(self):
        old_movie = {
            "id": 55,
            "title": "Old Stalled",
            "monitored": True,
            "hasFile": False,
            "isAvailable": True,
            "added": "2020-01-01T00:00:00Z",
            "images": [],
        }
        mock_client = MagicMock()
        mock_client.get_queue.return_value = []
        mock_client.get_movies.return_value = [old_movie]

        conn = self._mock_conn_with_radarr_setting()
        items = self._patched_arr_queue(conn, mock_client)

        titles = [i["title"] for i in items]
        assert "Old Stalled" in titles
        hit = next(i for i in items if i["title"] == "Old Stalled")
        assert hit["is_upcoming"] is False
        assert hit["arr_id"] == 55


from mediaman.services.download_queue import (
    _maybe_trigger_search,
    _reset_search_triggers,
    _last_search_trigger,
)


class TestSearchTriggerThrottle:
    def setup_method(self):
        _reset_search_triggers()

    def test_stale_released_movie_triggers_search(self, monkeypatch):
        """A monitored-no-file movie older than 5 min with no prior trigger fires MoviesSearch."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        def fake_build(c, svc):
            return mock_radarr if svc == "radarr" else None

        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client", fake_build
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Feel My Voice",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,  # 10 minutes ago
        }
        _maybe_trigger_search(conn, item, matched_nzb=False)

        mock_radarr.search_movie.assert_called_once_with(42)

    def test_second_call_within_15_min_does_not_trigger(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Feel My Voice",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _maybe_trigger_search(conn, item, matched_nzb=False)
        _maybe_trigger_search(conn, item, matched_nzb=False)
        assert mock_radarr.search_movie.call_count == 1

    def test_upcoming_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Future Movie",
            "arr_id": 7,
            "is_upcoming": True,
            "added_at": time.time() - 99999,
        }
        _maybe_trigger_search(conn, item, matched_nzb=False)
        mock_radarr.search_movie.assert_not_called()

    def test_recently_added_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Fresh Movie",
            "arr_id": 3,
            "is_upcoming": False,
            "added_at": time.time() - 60,  # 1 minute ago (below 5 min threshold)
        }
        _maybe_trigger_search(conn, item, matched_nzb=False)
        mock_radarr.search_movie.assert_not_called()

    def test_matched_nzb_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Actively Downloading",
            "arr_id": 11,
            "is_upcoming": False,
            "added_at": time.time() - 9999,
        }
        _maybe_trigger_search(conn, item, matched_nzb=True)
        mock_radarr.search_movie.assert_not_called()

    def test_series_triggers_search_series(self, monkeypatch):
        mock_sonarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: mock_sonarr if svc == "sonarr" else None,
        )

        import time

        item = {
            "kind": "series",
            "dl_id": "sonarr:Some Show",
            "arr_id": 77,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _maybe_trigger_search(conn, item, matched_nzb=False)
        mock_sonarr.search_series.assert_called_once_with(77)

    def test_trigger_after_16_min_fires_again(self, monkeypatch):
        """After the 15-min throttle expires, a second call fires again."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Dune",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _maybe_trigger_search(conn, item, matched_nzb=False)
        # Rewind the stored timestamp by 16 minutes
        _last_search_trigger["radarr:Dune"] = time.time() - 16 * 60
        _maybe_trigger_search(conn, item, matched_nzb=False)
        assert mock_radarr.search_movie.call_count == 2


from mediaman.services.download_queue import build_downloads_response as _build_downloads_response


class TestBuildDownloadsResponseBuckets:
    def setup_method(self):
        _reset_search_triggers()

    def test_response_has_upcoming_key(self, monkeypatch):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = None
        conn.execute.return_value = cursor

        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue", lambda c: []
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_nzbget_client", lambda c: None
        )

        resp = _build_downloads_response(conn)
        assert "hero" in resp
        assert "queue" in resp
        assert "upcoming" in resp
        assert "recent" in resp
        assert resp["upcoming"] == []

    def test_upcoming_item_goes_to_upcoming_bucket_not_queue(self, monkeypatch):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = None
        conn.execute.return_value = cursor

        upcoming_item = {
            "kind": "movie",
            "dl_id": "radarr:Future Film",
            "title": "Future Film",
            "source": "Radarr",
            "poster_url": "http://img/future.jpg",
            "progress": 0,
            "size": 0,
            "sizeleft": 0,
            "size_str": "0 B",
            "done_str": "0 B",
            "timeleft": "",
            "status": "searching",
            "arr_id": 7,
            "added_at": 0.0,
            "is_upcoming": True,
            "release_label": "Releases 14 Jun 2099",
        }
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue",
            lambda c: [upcoming_item],
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_nzbget_client", lambda c: None
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn)
        assert resp["hero"] is None
        assert resp["queue"] == []
        assert len(resp["upcoming"]) == 1
        assert resp["upcoming"][0]["title"] == "Future Film"
        assert resp["upcoming"][0]["state"] == "upcoming"
        assert resp["upcoming"][0]["release_label"] == "Releases 14 Jun 2099"

    def test_mixed_items_route_correctly(self, monkeypatch):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = None
        conn.execute.return_value = cursor

        released = {
            "kind": "movie",
            "dl_id": "radarr:Feel My Voice",
            "title": "Feel My Voice",
            "source": "Radarr",
            "poster_url": "",
            "progress": 0,
            "size": 0,
            "sizeleft": 0,
            "size_str": "0 B",
            "done_str": "0 B",
            "timeleft": "",
            "status": "searching",
            "arr_id": 42,
            "added_at": 0.0,
            "is_upcoming": False,
            "release_label": "",
        }
        upcoming = {
            "kind": "movie",
            "dl_id": "radarr:Hail Mary",
            "title": "Project Hail Mary",
            "source": "Radarr",
            "poster_url": "",
            "progress": 0,
            "size": 0,
            "sizeleft": 0,
            "size_str": "0 B",
            "done_str": "0 B",
            "timeleft": "",
            "status": "searching",
            "arr_id": 99,
            "added_at": 0.0,
            "is_upcoming": True,
            "release_label": "Not yet released",
        }
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue",
            lambda c: [released, upcoming],
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_nzbget_client", lambda c: None
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn)
        assert resp["hero"] is not None
        assert resp["hero"]["title"] == "Feel My Voice"
        assert resp["queue"] == []
        assert len(resp["upcoming"]) == 1
        assert resp["upcoming"][0]["title"] == "Project Hail Mary"


from mediaman.services.download_format import _looks_like_series_nzb


class TestLooksLikeSeriesNzb:
    def test_sxxexx_marker_matches(self):
        assert _looks_like_series_nzb("Love.Island.S06E13.1080p.WEB.mkv")

    def test_season_only_marker_matches(self):
        assert _looks_like_series_nzb("The.Great.S02.Complete.1080p")

    def test_movie_style_name_does_not_match(self):
        assert not _looks_like_series_nzb(
            "The.Great.Gatsby.2013.1080p.BluRay.x264.mkv"
        )

    def test_empty_string_does_not_match(self):
        assert not _looks_like_series_nzb("")


def _fake_nzbget_client(queue, status=None):
    client = MagicMock()
    client.get_queue.return_value = queue
    client.get_status.return_value = status or {"DownloadRate": 0}
    return client


def _fake_conn_empty_recent():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None
    conn.execute.return_value = cursor
    return conn


class TestNzbSeriesMatching:
    """Regression tests for the multi-episode / movie-steals-series bugs."""

    def setup_method(self):
        _reset_previous_queue()
        _reset_search_triggers()

    def test_multiple_episodes_of_same_series_do_not_leak_as_movies(
        self, monkeypatch
    ):
        """Four NZBs for the same series must collapse into one series card.

        Before the dedup fix, nzb_title_map overwrote entries with the same
        cleaned title, leaving sibling episodes unmatched → they rendered as
        poster-less "movie" cards.
        """
        conn = _fake_conn_empty_recent()

        arr_series = {
            "kind": "series",
            "dl_id": "sonarr:The Great",
            "title": "The Great",
            "source": "Sonarr",
            "poster_url": "http://img/great.jpg",
            "episodes": [
                {"label": "S01E01", "title": "Ep1", "progress": 80,
                 "size": 5_000_000_000, "sizeleft": 1_000_000_000,
                 "size_str": "5 GB", "status": "downloading"},
                {"label": "S01E02", "title": "Ep2", "progress": 90,
                 "size": 5_000_000_000, "sizeleft": 500_000_000,
                 "size_str": "5 GB", "status": "downloading"},
                {"label": "S01E03", "title": "Ep3", "progress": 95,
                 "size": 5_000_000_000, "sizeleft": 250_000_000,
                 "size_str": "5 GB", "status": "downloading"},
                {"label": "S01E04", "title": "Ep4", "progress": 70,
                 "size": 5_000_000_000, "sizeleft": 1_500_000_000,
                 "size_str": "5 GB", "status": "downloading"},
            ],
            "episode_count": 4,
            "downloading_count": 4,
            "progress": 83,
            "size": 20_000_000_000,
            "sizeleft": 3_250_000_000,
            "size_str": "20 GB",
            "done_str": "16.7 GB",
            "is_upcoming": False,
            "release_label": "",
            "arr_id": 11,
            "added_at": 0.0,
        }
        nzb_queue = [
            {"NZBName": f"The.Great.S01E0{i}.1080p.WEB.x264.mkv",
             "FileSizeMB": 5000, "RemainingSizeMB": rem, "Status": "DOWNLOADING"}
            for i, rem in enumerate([1000, 500, 250, 1500], start=1)
        ]

        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue",
            lambda c: [arr_series],
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_nzbget_client",
            lambda c: _fake_nzbget_client(nzb_queue),
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn)

        all_items = [resp["hero"]] + (resp["queue"] or [])
        all_items = [i for i in all_items if i is not None]
        assert len(all_items) == 1, (
            f"Expected one series card, got {len(all_items)}: "
            f"{[(i['title'], i['media_type']) for i in all_items]}"
        )
        assert all_items[0]["media_type"] == "series"
        assert all_items[0]["title"] == "The Great"
        assert all_items[0]["poster_url"] == "http://img/great.jpg"

    def test_movie_arr_does_not_steal_series_episode_nzb(self, monkeypatch):
        """A Radarr movie whose title is a substring of a TV show title
        must not claim the series' NZBs via the loose substring match.
        """
        conn = _fake_conn_empty_recent()

        arr_movie = {
            "kind": "movie",
            "dl_id": "radarr:The Greatest Showman",
            "title": "The Greatest Showman",
            "source": "Radarr",
            "poster_url": "http://img/showman.jpg",
            "progress": 0,
            "size": 0, "sizeleft": 0,
            "size_str": "0 B", "done_str": "0 B",
            "timeleft": "", "status": "searching",
            "arr_id": 101, "added_at": 0.0,
            "is_upcoming": False, "release_label": "",
        }
        arr_series = {
            "kind": "series",
            "dl_id": "sonarr:The Great",
            "title": "The Great",
            "source": "Sonarr",
            "poster_url": "http://img/great.jpg",
            "episodes": [
                {"label": "S01E01", "title": "Ep1", "progress": 80,
                 "size": 5_000_000_000, "sizeleft": 1_000_000_000,
                 "size_str": "5 GB", "status": "downloading"},
            ],
            "episode_count": 1, "downloading_count": 1,
            "progress": 80,
            "size": 5_000_000_000, "sizeleft": 1_000_000_000,
            "size_str": "5 GB", "done_str": "4 GB",
            "is_upcoming": False, "release_label": "",
            "arr_id": 11, "added_at": 0.0,
        }
        nzb_queue = [
            {"NZBName": "The.Great.S01E01.1080p.WEB.x264.mkv",
             "FileSizeMB": 5000, "RemainingSizeMB": 1000,
             "Status": "DOWNLOADING"},
        ]

        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue",
            lambda c: [arr_movie, arr_series],  # Radarr iterated first
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_nzbget_client",
            lambda c: _fake_nzbget_client(nzb_queue),
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn)

        series_items = [
            i for i in [resp["hero"]] + (resp["queue"] or [])
            if i and i["title"] == "The Great"
        ]
        assert len(series_items) == 1
        assert series_items[0]["media_type"] == "series"
        assert series_items[0]["state"] == "downloading", (
            "Series should have matched the NZB, not fallen through to "
            f"searching: {series_items[0]}"
        )

    def test_unmatched_series_nzb_renders_as_series(self, monkeypatch):
        """An NZB with SxxExx marker and no arr match still renders as
        series, not the default hardcoded 'movie'.
        """
        conn = _fake_conn_empty_recent()
        nzb_queue = [
            {"NZBName": "Some.Orphan.Show.S02E05.1080p.WEB.mkv",
             "FileSizeMB": 3000, "RemainingSizeMB": 500,
             "Status": "DOWNLOADING"},
        ]

        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue", lambda c: []
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_nzbget_client",
            lambda c: _fake_nzbget_client(nzb_queue),
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn)
        assert resp["hero"] is not None
        assert resp["hero"]["media_type"] == "series"

    def test_punctuation_drift_still_matches_series(self, monkeypatch):
        """Sonarr title "Married at First Sight (AU)" must still claim its
        NZBs after the parens are stripped from the cleaned NZB name.

        Before the normalise-for-match fix, the substring check compared
        "married at first sight (au)" (arr) with "married at first sight au"
        (nzb). Neither string contains the other, so the series card was
        orphaned and every episode NZB leaked through as its own card.
        """
        conn = _fake_conn_empty_recent()
        arr_series = {
            "kind": "series",
            "dl_id": "sonarr:Married at First Sight (AU)",
            "title": "Married at First Sight (AU)",
            "source": "Sonarr",
            "poster_url": "http://img/mafs.jpg",
            "episodes": [
                {"label": f"S12E{i:02d}", "title": f"Ep{i}", "progress": 40,
                 "size": 2_500_000_000, "sizeleft": 1_500_000_000,
                 "size_str": "2.5 GB", "status": "downloading"}
                for i in range(1, 4)
            ],
            "episode_count": 3, "downloading_count": 3,
            "progress": 40,
            "size": 7_500_000_000, "sizeleft": 4_500_000_000,
            "size_str": "7.5 GB", "done_str": "3 GB",
            "is_upcoming": False, "release_label": "",
            "arr_id": 42, "added_at": 0.0,
        }
        nzb_queue = [
            {"NZBName": f"Married.at.First.Sight.AU.S12E{i:02d}.1080p.WEBRip.x264",
             "FileSizeMB": 2500, "RemainingSizeMB": 1500,
             "Status": "DOWNLOADING"}
            for i in range(1, 4)
        ]

        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue",
            lambda c: [arr_series],
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_nzbget_client",
            lambda c: _fake_nzbget_client(nzb_queue),
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn)
        all_items = [resp["hero"]] + (resp["queue"] or [])
        all_items = [i for i in all_items if i is not None]
        assert len(all_items) == 1, (
            "Episodes leaked as separate cards — titles: "
            f"{[(i['title'], i['media_type']) for i in all_items]}"
        )
        card = all_items[0]
        assert card["media_type"] == "series"
        assert card["title"] == "Married at First Sight (AU)"
        assert card["poster_url"] == "http://img/mafs.jpg"
        assert card["state"] == "downloading"


from mediaman.services.download_queue import trigger_pending_searches


class TestTriggerPendingSearches:
    def setup_method(self):
        _reset_search_triggers()

    def test_iterates_arr_items_and_pokes_search(self, monkeypatch):
        """Scheduler job walks every arr item and calls _maybe_trigger_search."""
        conn = MagicMock()
        items = [
            {"kind": "movie", "dl_id": "radarr:A", "arr_id": 1, "is_upcoming": False, "added_at": 0},
            {"kind": "series", "dl_id": "sonarr:B", "arr_id": 2, "is_upcoming": False, "added_at": 0},
        ]
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue",
            lambda c: items,
        )
        calls: list[tuple] = []
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda c, i, matched_nzb: calls.append((i["dl_id"], matched_nzb)),
        )

        trigger_pending_searches(conn)

        assert calls == [("radarr:A", False), ("sonarr:B", False)]

    def test_swallows_arr_queue_fetch_failure(self, monkeypatch):
        """If fetching the arr queue blows up, the scheduler job does not propagate."""
        conn = MagicMock()

        def boom(c):
            raise RuntimeError("radarr down")

        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue", boom
        )
        # Sonarr pass still runs — stub it out so the test is deterministic.
        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: None,
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda *a, **kw: called.append(a),
        )

        trigger_pending_searches(conn)

        assert called == []

    def test_sonarr_partial_missing_pokes_only_new_series(self, monkeypatch):
        """Series returned by Sonarr wanted/missing fire SeriesSearch unless
        already covered by the main pass."""
        conn = MagicMock()

        # Main pass surfaces one zero-file series (id=1).
        arr_items = [
            {"kind": "series", "dl_id": "sonarr:Already", "arr_id": 1,
             "is_upcoming": False, "added_at": 0},
        ]
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue",
            lambda c: arr_items,
        )

        # Sonarr client returns id=1 (dup) and id=2 (partial missing, new).
        mock_sonarr = MagicMock()
        mock_sonarr.get_missing_series.return_value = {
            1: "Already",
            2: "Partial Show",
        }
        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: mock_sonarr if svc == "sonarr" else None,
        )

        calls: list[tuple] = []
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda c, i, matched_nzb: calls.append((i["dl_id"], i["arr_id"])),
        )

        trigger_pending_searches(conn)

        # One call from the main pass, one from the partial-missing pass.
        assert calls == [("sonarr:Already", 1), ("sonarr:Partial Show", 2)]

    def test_sonarr_partial_missing_skipped_when_client_missing(self, monkeypatch):
        conn = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.download_queue._get_arr_queue",
            lambda c: [],
        )
        monkeypatch.setattr(
            "mediaman.services.download_queue._build_arr_client",
            lambda c, svc: None,
        )
        calls = []
        monkeypatch.setattr(
            "mediaman.services.download_queue._maybe_trigger_search",
            lambda *a, **kw: calls.append(a),
        )

        trigger_pending_searches(conn)

        assert calls == []
