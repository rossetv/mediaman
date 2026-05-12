"""Tests for the redesigned downloads API."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import requests

from mediaman.db import init_db
from tests.helpers.factories import insert_recent_download


class TestRecentDownloadsTable:
    def test_recent_downloads_table_exists(self, db_path):
        """Migration v9 creates the recent_downloads table."""
        conn = init_db(str(db_path))
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        assert "recent_downloads" in tables

    def test_recent_downloads_columns(self, db_path):
        """recent_downloads has the expected columns."""
        conn = init_db(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(recent_downloads)").fetchall()]
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


from mediaman.services.downloads.download_format import (  # noqa: E402
    build_item,
    map_state,
    select_hero,
)


class TestStateMapping:
    def test_searching_state(self):
        """Item in Arr queue with no NZBGet match → searching."""
        assert map_state(nzbget_status=None, has_nzbget_match=False) == "searching"

    def test_downloading_state(self):
        """NZBGet status contains DOWNLOADING → downloading."""
        assert map_state(nzbget_status="DOWNLOADING", has_nzbget_match=True) == "downloading"

    def test_almost_ready_unpacking(self):
        """NZBGet status contains UNPACKING → almost_ready."""
        assert map_state(nzbget_status="UNPACKING", has_nzbget_match=True) == "almost_ready"

    def test_almost_ready_postprocessing(self):
        """NZBGet status contains PP_ → almost_ready."""
        assert map_state(nzbget_status="PP_QUEUED", has_nzbget_match=True) == "almost_ready"

    def test_queued_state(self):
        """NZBGet status is QUEUED → searching (not yet actively downloading)."""
        assert map_state(nzbget_status="QUEUED", has_nzbget_match=True) == "searching"

    def test_paused_state(self):
        """NZBGet status is PAUSED → downloading (still has progress)."""
        assert map_state(nzbget_status="PAUSED", has_nzbget_match=True) == "downloading"


class TestBuildItem:
    def test_movie_item_shape(self):
        """A movie item has the expected fields."""
        item = build_item(
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
        item = build_item(
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
        item = build_item(
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
        item = build_item(
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
        items = [
            build_item(
                dl_id="r:A",
                title="A",
                media_type="movie",
                poster_url="",
                state="downloading",
                progress=50,
                eta="",
                size_done="",
                size_total="",
            )
        ]
        hero, rest = select_hero(items)
        assert hero["id"] == "r:A"
        assert rest == []

    def test_highest_progress_downloading_is_hero(self):
        """The actively downloading item with the highest progress wins."""
        items = [
            build_item(
                dl_id="r:A",
                title="A",
                media_type="movie",
                poster_url="",
                state="searching",
                progress=0,
                eta="",
                size_done="",
                size_total="",
            ),
            build_item(
                dl_id="r:B",
                title="B",
                media_type="movie",
                poster_url="",
                state="downloading",
                progress=30,
                eta="",
                size_done="",
                size_total="",
            ),
            build_item(
                dl_id="r:C",
                title="C",
                media_type="movie",
                poster_url="",
                state="downloading",
                progress=80,
                eta="",
                size_done="",
                size_total="",
            ),
        ]
        hero, rest = select_hero(items)
        assert hero["id"] == "r:C"
        assert len(rest) == 2

    def test_no_downloading_picks_first(self):
        """When all items are searching, the first item is the hero."""
        items = [
            build_item(
                dl_id="r:A",
                title="A",
                media_type="movie",
                poster_url="",
                state="searching",
                progress=0,
                eta="",
                size_done="",
                size_total="",
            ),
            build_item(
                dl_id="r:B",
                title="B",
                media_type="movie",
                poster_url="",
                state="searching",
                progress=0,
                eta="",
                size_done="",
                size_total="",
            ),
        ]
        hero, rest = select_hero(items)
        assert hero["id"] == "r:A"
        assert len(rest) == 1

    def test_empty_queue_returns_none(self):
        """Empty queue returns None hero."""
        hero, rest = select_hero([])
        assert hero is None
        assert rest == []


from mediaman.services.arr.completion import detect_completed  # noqa: E402
from mediaman.services.downloads.download_queue import _reset_previous_queue  # noqa: E402


class TestCompletionDetection:
    def setup_method(self):
        """Reset state between tests."""
        _reset_previous_queue()

    def test_item_disappearing_is_completed(self):
        """An item present previously but absent now is detected as completed."""
        previous = {
            "radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "kind": "movie", "poster_url": ""}
        }
        current = {}
        completed = detect_completed(previous, current)
        assert len(completed) == 1
        assert completed[0]["dl_id"] == "radarr:Dune"

    def test_no_change_means_no_completions(self):
        """Same items in both snapshots → nothing completed."""
        snapshot = {
            "radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "kind": "movie", "poster_url": ""}
        }
        completed = detect_completed(snapshot, snapshot)
        assert completed == []

    def test_new_item_is_not_completed(self):
        """An item appearing for the first time is not a completion."""
        previous = {}
        current = {
            "radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "kind": "movie", "poster_url": ""}
        }
        completed = detect_completed(previous, current)
        assert completed == []

    def test_reset_clears_previous(self):
        """_reset_previous_queue clears the in-memory snapshot."""
        _reset_previous_queue()  # Should not raise


from unittest.mock import MagicMock, patch  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from mediaman.config import Config  # noqa: E402
from mediaman.db import set_connection  # noqa: E402
from mediaman.web.auth.password_hash import create_user  # noqa: E402
from mediaman.web.auth.session_store import create_session  # noqa: E402
from mediaman.web.routes.download import router as download_router  # noqa: E402


def _make_download_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(download_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


class TestDownloadStatusAPI:
    def setup_method(self):
        """Clear the per-(service, tmdb_id) status cache so each test sees
        fresh upstream calls instead of replaying a previous test's payload."""
        from mediaman.web.routes.download import reset_download_caches

        reset_download_caches()

    def test_status_returns_new_shape_fields(self, db_path, secret_key):
        """GET /api/download/status returns the new item shape with state field."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
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

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=123")

        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert data["state"] == "downloading"
        assert "progress" in data
        assert "eta" in data
        assert "episodes" in data

    def test_status_invalid_service_returns_422(self, db_path, secret_key):
        """An empty / unknown ``service`` is rejected at the FastAPI layer.

        Previously the route silently fell through to an "unknown" item
        for any service string; the route now constrains ``service`` to
        ``Literal["radarr", "sonarr"]`` and ``tmdb_id`` to ``gt=0`` so
        malformed callers get a clear 422 rather than a placeholder
        success response."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        resp = client.get("/api/download/status?service=&tmdb_id=0")
        assert resp.status_code == 422

    def test_status_radarr_ready(self, db_path, secret_key):
        """Movie with hasFile=True returns state=ready with progress=100."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
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

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=42")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ready"
        assert data["progress"] == 100

    def test_status_radarr_searching(self, db_path, secret_key):
        """Movie not in queue and no file → state=searching."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = None
        mock_client.get_queue.return_value = []

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=999")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "searching"

    def test_admin_is_rate_limited(self, db_path, secret_key):
        """An admin session must NOT bypass the download-status rate
        limit. A stored XSS firing under an admin cookie (or a rogue
        admin) could otherwise hammer the endpoint uncapped."""
        from mediaman.web.routes import download as download_mod

        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        # Reset the module-level limiter so earlier tests don't
        # contaminate our bucket.
        download_mod._DOWNLOAD_STATUS_LIMITER._attempts.clear()

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {
            "hasFile": True,
            "title": "Dune",
            "tmdbId": 1,
            "images": [],
        }

        cap = download_mod._DOWNLOAD_STATUS_LIMITER._max_attempts

        # Burn through the whole window.
        try:
            with patch(
                "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
            ):
                for _ in range(cap):
                    r = client.get("/api/download/status?service=radarr&tmdb_id=1")
                    assert r.status_code == 200

                # Next admin call must be rejected — no bypass.
                r = client.get("/api/download/status?service=radarr&tmdb_id=1")

            assert r.status_code == 429
            assert r.json().get("error") == "too_many_requests"
        finally:
            # Leave the limiter clean so later tests aren't poisoned.
            download_mod._DOWNLOAD_STATUS_LIMITER._attempts.clear()

    def test_status_timeleft_formatting(self, db_path, secret_key):
        """timeleft HH:MM:SS is formatted as human-readable eta."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
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

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=77")

        assert resp.status_code == 200
        data = resp.json()
        assert "1 hr" in data["eta"]
        assert "remaining" in data["eta"]

    def test_status_safehttperror_returns_unknown_not_500(self, db_path, secret_key):
        """A SafeHTTPError from an Arr 5xx must surface as the 'unknown' state,
        not propagate as an unhandled exception → HTTP 500 to the client."""
        from mediaman.services.infra.http import SafeHTTPError

        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.side_effect = SafeHTTPError(
            status_code=500, body_snippet="boom", url="http://radarr.local"
        )

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=88")

        assert resp.status_code == 200
        assert resp.json()["state"] == "unknown"

    def test_status_progress_clamps_to_100(self, db_path, secret_key):
        """A misreported sizeleft larger than size must not produce a negative
        progress value or one above 100. Clamp to [0, 100]."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        # sizeleft (10 GB) > size (5 GB) → naive math yields -100% progress.
        # The clamp must coerce that to 0.
        mock_queue_item = {
            "movie": {"title": "Bad", "tmdbId": 33, "images": []},
            "size": 5_000_000_000,
            "sizeleft": 10_000_000_000,
            "status": "downloading",
            "trackedDownloadStatus": "downloading",
            "timeleft": "00:00:30",
        }
        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {"hasFile": False, "tmdbId": 33}
        mock_client.get_queue.return_value = [mock_queue_item]

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=33")

        assert resp.status_code == 200
        data = resp.json()
        assert 0 <= data["progress"] <= 100

    def test_status_string_size_does_not_crash(self, db_path, secret_key):
        """An Arr response with a stringified size must not raise TypeError —
        the safe-int coercion treats the field as zero and returns sensibly."""
        conn = init_db(str(db_path))
        app = _make_download_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)

        # A malformed Arr response with size as a string (which used to make
        # ``size_total > 0`` raise TypeError) must round-trip cleanly.
        mock_queue_item = {
            "movie": {"title": "Test", "tmdbId": 55, "images": []},
            "size": "not-a-number",
            "sizeleft": "also-bad",
            "status": "downloading",
            "trackedDownloadStatus": "downloading",
            "timeleft": "00:01:00",
        }
        mock_client = MagicMock()
        mock_client.get_movie_by_tmdb.return_value = {"hasFile": False, "tmdbId": 55}
        mock_client.get_queue.return_value = [mock_queue_item]

        with patch(
            "mediaman.web.routes.download.status.build_radarr_from_db", return_value=mock_client
        ):
            resp = client.get("/api/download/status?service=radarr&tmdb_id=55")

        assert resp.status_code == 200
        data = resp.json()
        assert data["progress"] == 0


class TestRecentDownloadsCleanup:
    def test_cleanup_removes_old_rows(self, db_path):
        """Rows older than 7 days are purged."""
        conn = init_db(str(db_path))
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        # Insert a row dated 10 days ago
        insert_recent_download(conn, dl_id="radarr:Old", title="Old Movie", media_type="movie", completed_at=ten_days_ago)
        # Insert a row from today
        insert_recent_download(conn, dl_id="radarr:New", title="New Movie", media_type="movie")

        from mediaman.services.arr.completion import cleanup_recent_downloads

        cleanup_recent_downloads(conn)

        rows = conn.execute("SELECT dl_id FROM recent_downloads").fetchall()
        dl_ids = [r["dl_id"] for r in rows]
        assert "radarr:Old" not in dl_ids
        assert "radarr:New" in dl_ids


from mediaman.services.downloads.download_format import (  # noqa: E402
    classify_movie_upcoming,
    classify_series_upcoming,
)


class TestClassifyMovieUpcoming:
    def test_not_available_movie_is_upcoming(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "digitalRelease": "2099-06-14T00:00:00Z",
        }
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert label.startswith("Releases ")
        assert "2099" in label

    def test_available_movie_is_not_upcoming(self):
        movie = {"monitored": True, "hasFile": False, "isAvailable": True}
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is False
        assert label == ""

    def test_unmonitored_movie_is_not_upcoming(self):
        movie = {"monitored": False, "hasFile": False, "isAvailable": False}
        is_upcoming, _label = classify_movie_upcoming(movie)
        assert is_upcoming is False

    def test_already_has_file_is_not_upcoming(self):
        movie = {"monitored": True, "hasFile": True, "isAvailable": False}
        is_upcoming, _label = classify_movie_upcoming(movie)
        assert is_upcoming is False

    def test_upcoming_with_no_release_dates_has_fallback_label(self):
        movie = {"monitored": True, "hasFile": False, "isAvailable": False}
        is_upcoming, label = classify_movie_upcoming(movie)
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
        is_upcoming, label = classify_movie_upcoming(movie)
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
        is_upcoming, label = classify_movie_upcoming(movie)
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
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert label == "Not yet released"


class TestClassifySeriesUpcoming:
    def test_upcoming_status_is_upcoming(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 0},
        }
        is_upcoming, _label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is True

    def test_continuing_with_aired_episodes_is_not_upcoming(self):
        series = {
            "monitored": True,
            "status": "continuing",
            "statistics": {"episodeFileCount": 0},
        }
        episodes = [{"airDateUtc": "2020-01-01T00:00:00Z"}]
        is_upcoming, _label = classify_series_upcoming(series, episodes=episodes)
        assert is_upcoming is False

    def test_unmonitored_is_not_upcoming(self):
        series = {"monitored": False, "status": "upcoming"}
        is_upcoming, _label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False

    def test_has_episode_files_is_not_upcoming(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 3},
        }
        is_upcoming, _label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False

    def test_all_future_episodes_with_continuing_status_is_upcoming(self):
        series = {
            "monitored": True,
            "status": "continuing",
            "statistics": {"episodeFileCount": 0},
        }
        episodes = [{"airDateUtc": "2099-12-01T00:00:00Z"}]
        is_upcoming, label = classify_series_upcoming(series, episodes=episodes)
        assert is_upcoming is True
        assert "2099" in label
        assert label.startswith("Premieres ")

    def test_upcoming_label_with_no_air_dates_has_fallback(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 0},
        }
        is_upcoming, label = classify_series_upcoming(series, episodes=[])
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
        is_upcoming, label = classify_series_upcoming(series, episodes=episodes)
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
        is_upcoming, label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False
        assert label == ""


from mediaman.services.arr.fetcher import fetch_arr_queue as _get_arr_queue  # noqa: E402


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
        """Run _get_arr_queue with build_radarr_from_db patched."""
        with patch("mediaman.services.arr.build.build_radarr_from_db", return_value=mock_client):
            return _get_arr_queue(conn, "test-secret-key-for-unit-tests-only")

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


from mediaman.services.arr.search_trigger import (  # noqa: E402
    _last_search_trigger,
    maybe_trigger_search,
    reset_search_triggers,
)


class TestSearchTriggerThrottle:
    def setup_method(self):
        reset_search_triggers()

    def test_stale_released_movie_triggers_search(self, monkeypatch):
        """A monitored-no-file movie older than 5 min with no prior trigger fires MoviesSearch."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Feel My Voice",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,  # 10 minutes ago
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")

        mock_radarr.search_movie.assert_called_once_with(42)

    def test_second_call_within_two_minutes_does_not_trigger(self, monkeypatch):
        """After the first fire, the per-dl_id backoff gate is 2 min — anything sooner is dropped."""
        mock_radarr = MagicMock()
        conn = MagicMock()
        # Make the DB appear empty so no persisted count inflates previous_count.
        conn.execute.return_value.fetchone.return_value = None
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )
        # Pin jitter so the gate is exactly 120 s, not [108, 132].
        from mediaman.services.arr import _throttle_state as _ts

        monkeypatch.setattr(_ts._SEARCH_BACKOFF, "deterministic_multiplier", lambda seed: 1.0)

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Backoff Test",
            "arr_id": 99,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        # Fire #1 — wide-open gate.
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 1
        # Fire #2 immediately — gated by interval(1) = 120 s.
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 1

    def test_backoff_curve_advances_through_steps(self, monkeypatch):
        """Once each backoff window passes, the next call fires; doubles each step."""
        mock_radarr = MagicMock()
        conn = MagicMock()
        # Make the DB appear empty so no persisted count inflates previous_count.
        conn.execute.return_value.fetchone.return_value = None
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )
        from mediaman.services.arr import _throttle_state as _ts

        monkeypatch.setattr(_ts._SEARCH_BACKOFF, "deterministic_multiplier", lambda seed: 1.0)

        from mediaman.services.arr import search_trigger as st

        clock = [1700000000.0]
        monkeypatch.setattr(st.time, "time", lambda: clock[0])

        item = {
            "kind": "movie",
            "dl_id": "radarr:Backoff Cycle",
            "arr_id": 100,
            "is_upcoming": False,
            "added_at": clock[0] - 600,
        }
        st.maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 1

        # Advance past the 2-min interval(1) gate.
        clock[0] += 121
        st.maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 2

        # Now interval(2) = 4 min. 121 s isn't enough.
        clock[0] += 121
        st.maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 2

        # Advance past 4 min total.
        clock[0] += 240 + 1
        st.maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 3

    def test_upcoming_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Future Movie",
            "arr_id": 7,
            "is_upcoming": True,
            "added_at": time.time() - 99999,
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        mock_radarr.search_movie.assert_not_called()

    def test_recently_added_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Fresh Movie",
            "arr_id": 3,
            "is_upcoming": False,
            "added_at": time.time() - 60,  # 1 minute ago (below 5 min threshold)
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        mock_radarr.search_movie.assert_not_called()

    def test_matched_nzb_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Actively Downloading",
            "arr_id": 11,
            "is_upcoming": False,
            "added_at": time.time() - 9999,
        }
        maybe_trigger_search(conn, item, matched_nzb=True, secret_key="test-key")
        mock_radarr.search_movie.assert_not_called()

    def test_series_triggers_search_series(self, monkeypatch):
        mock_sonarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: mock_sonarr,
        )

        import time

        item = {
            "kind": "series",
            "dl_id": "sonarr:Some Show",
            "arr_id": 77,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        mock_sonarr.search_series.assert_called_once_with(77)

    def test_trigger_after_16_min_fires_again(self, monkeypatch):
        """After the 15-min throttle expires, a second call fires again."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Dune",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        # Rewind the stored timestamp by 16 minutes
        _last_search_trigger["radarr:Dune"] = time.time() - 16 * 60
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 2


from mediaman.services.downloads.download_queue import (  # noqa: E402
    build_downloads_response as _build_downloads_response,
)


class TestBuildDownloadsResponseBuckets:
    def setup_method(self):
        reset_search_triggers()

    def test_response_has_upcoming_key(self, monkeypatch):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = None
        conn.execute.return_value = cursor

        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.fetch_arr_queue", lambda c, _sk: []
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db", lambda c, _sk: None
        )

        resp = _build_downloads_response(conn, "test-key")
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
            "mediaman.services.downloads.download_queue.fetch_arr_queue",
            lambda c, _sk: [upcoming_item],
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db", lambda c, _sk: None
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn, "test-key")
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
            "mediaman.services.downloads.download_queue.fetch_arr_queue",
            lambda c, _sk: [released, upcoming],
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db", lambda c, _sk: None
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn, "test-key")
        assert resp["hero"] is not None
        assert resp["hero"]["title"] == "Feel My Voice"
        assert resp["queue"] == []
        assert len(resp["upcoming"]) == 1
        assert resp["upcoming"][0]["title"] == "Project Hail Mary"


from mediaman.services.downloads.download_format import looks_like_series_nzb  # noqa: E402


class TestLooksLikeSeriesNzb:
    def test_sxxexx_marker_matches(self):
        assert looks_like_series_nzb("Love.Island.S06E13.1080p.WEB.mkv")

    def test_season_only_marker_matches(self):
        assert looks_like_series_nzb("The.Great.S02.Complete.1080p")

    def test_movie_style_name_does_not_match(self):
        assert not looks_like_series_nzb("The.Great.Gatsby.2013.1080p.BluRay.x264.mkv")

    def test_empty_string_does_not_match(self):
        assert not looks_like_series_nzb("")


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
        reset_search_triggers()

    def test_multiple_episodes_of_same_series_do_not_leak_as_movies(self, monkeypatch):
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
                {
                    "label": "S01E01",
                    "title": "Ep1",
                    "progress": 80,
                    "size": 5_000_000_000,
                    "sizeleft": 1_000_000_000,
                    "size_str": "5 GB",
                    "status": "downloading",
                },
                {
                    "label": "S01E02",
                    "title": "Ep2",
                    "progress": 90,
                    "size": 5_000_000_000,
                    "sizeleft": 500_000_000,
                    "size_str": "5 GB",
                    "status": "downloading",
                },
                {
                    "label": "S01E03",
                    "title": "Ep3",
                    "progress": 95,
                    "size": 5_000_000_000,
                    "sizeleft": 250_000_000,
                    "size_str": "5 GB",
                    "status": "downloading",
                },
                {
                    "label": "S01E04",
                    "title": "Ep4",
                    "progress": 70,
                    "size": 5_000_000_000,
                    "sizeleft": 1_500_000_000,
                    "size_str": "5 GB",
                    "status": "downloading",
                },
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
            {
                "NZBName": f"The.Great.S01E0{i}.1080p.WEB.x264.mkv",
                "FileSizeMB": 5000,
                "RemainingSizeMB": rem,
                "Status": "DOWNLOADING",
            }
            for i, rem in enumerate([1000, 500, 250, 1500], start=1)
        ]

        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.fetch_arr_queue",
            lambda c, _sk: [arr_series],
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db",
            lambda c, _sk: _fake_nzbget_client(nzb_queue),
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn, "test-key")

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
            "size": 0,
            "sizeleft": 0,
            "size_str": "0 B",
            "done_str": "0 B",
            "timeleft": "",
            "status": "searching",
            "arr_id": 101,
            "added_at": 0.0,
            "is_upcoming": False,
            "release_label": "",
        }
        arr_series = {
            "kind": "series",
            "dl_id": "sonarr:The Great",
            "title": "The Great",
            "source": "Sonarr",
            "poster_url": "http://img/great.jpg",
            "episodes": [
                {
                    "label": "S01E01",
                    "title": "Ep1",
                    "progress": 80,
                    "size": 5_000_000_000,
                    "sizeleft": 1_000_000_000,
                    "size_str": "5 GB",
                    "status": "downloading",
                },
            ],
            "episode_count": 1,
            "downloading_count": 1,
            "progress": 80,
            "size": 5_000_000_000,
            "sizeleft": 1_000_000_000,
            "size_str": "5 GB",
            "done_str": "4 GB",
            "is_upcoming": False,
            "release_label": "",
            "arr_id": 11,
            "added_at": 0.0,
        }
        nzb_queue = [
            {
                "NZBName": "The.Great.S01E01.1080p.WEB.x264.mkv",
                "FileSizeMB": 5000,
                "RemainingSizeMB": 1000,
                "Status": "DOWNLOADING",
            },
        ]

        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.fetch_arr_queue",
            lambda c, _sk: [arr_movie, arr_series],  # Radarr iterated first
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db",
            lambda c, _sk: _fake_nzbget_client(nzb_queue),
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn, "test-key")

        series_items = [
            i for i in [resp["hero"]] + (resp["queue"] or []) if i and i["title"] == "The Great"
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
            {
                "NZBName": "Some.Orphan.Show.S02E05.1080p.WEB.mkv",
                "FileSizeMB": 3000,
                "RemainingSizeMB": 500,
                "Status": "DOWNLOADING",
            },
        ]

        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.fetch_arr_queue", lambda c, _sk: []
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db",
            lambda c, _sk: _fake_nzbget_client(nzb_queue),
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn, "test-key")
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
                {
                    "label": f"S12E{i:02d}",
                    "title": f"Ep{i}",
                    "progress": 40,
                    "size": 2_500_000_000,
                    "sizeleft": 1_500_000_000,
                    "size_str": "2.5 GB",
                    "status": "downloading",
                }
                for i in range(1, 4)
            ],
            "episode_count": 3,
            "downloading_count": 3,
            "progress": 40,
            "size": 7_500_000_000,
            "sizeleft": 4_500_000_000,
            "size_str": "7.5 GB",
            "done_str": "3 GB",
            "is_upcoming": False,
            "release_label": "",
            "arr_id": 42,
            "added_at": 0.0,
        }
        nzb_queue = [
            {
                "NZBName": f"Married.at.First.Sight.AU.S12E{i:02d}.1080p.WEBRip.x264",
                "FileSizeMB": 2500,
                "RemainingSizeMB": 1500,
                "Status": "DOWNLOADING",
            }
            for i in range(1, 4)
        ]

        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.fetch_arr_queue",
            lambda c, _sk: [arr_series],
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db",
            lambda c, _sk: _fake_nzbget_client(nzb_queue),
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.maybe_trigger_search",
            lambda *a, **kw: None,
        )

        resp = _build_downloads_response(conn, "test-key")
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


from mediaman.web.routes.downloads import router as downloads_router  # noqa: E402


def _make_downloads_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(downloads_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


from mediaman.services.arr.search_trigger import trigger_pending_searches  # noqa: E402


class TestTriggerPendingSearches:
    def setup_method(self):
        reset_search_triggers()

    def test_iterates_arr_items_and_pokes_search(self, monkeypatch):
        """Scheduler job walks every arr item and calls maybe_trigger_search."""
        conn = MagicMock()
        items = [
            {
                "kind": "movie",
                "dl_id": "radarr:A",
                "arr_id": 1,
                "is_upcoming": False,
                "added_at": 0,
            },
            {
                "kind": "series",
                "dl_id": "sonarr:B",
                "arr_id": 2,
                "is_upcoming": False,
                "added_at": 0,
            },
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.fetch_arr_queue",
            lambda c, _sk: items,
        )
        calls: list[tuple] = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda c, i, matched_nzb, **kw: calls.append((i["dl_id"], matched_nzb)),
        )

        trigger_pending_searches(conn, secret_key="test-key")

        assert calls == [("radarr:A", False), ("sonarr:B", False)]

    def test_swallows_arr_queue_fetch_failure(self, monkeypatch):
        """If fetching the arr queue blows up, the scheduler job does not propagate."""
        conn = MagicMock()

        def boom(c, sk):

            raise requests.ConnectionError("radarr down")

        monkeypatch.setattr("mediaman.services.arr.search_trigger.fetch_arr_queue", boom)
        # Sonarr pass still runs — stub it out so the test is deterministic.
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda *a, **kw: called.append(a),
        )

        trigger_pending_searches(conn, secret_key="test-key")

        assert called == []

    def test_sonarr_partial_missing_pokes_only_new_series(self, monkeypatch):
        """Series returned by Sonarr wanted/missing fire SeriesSearch unless
        already covered by the main pass."""
        conn = MagicMock()

        # Main pass surfaces one zero-file series (id=1).
        arr_items = [
            {
                "kind": "series",
                "dl_id": "sonarr:Already",
                "arr_id": 1,
                "is_upcoming": False,
                "added_at": 0,
            },
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.fetch_arr_queue",
            lambda c, _sk: arr_items,
        )

        # Sonarr client returns id=1 (dup) and id=2 (partial missing, new).
        mock_sonarr = MagicMock()
        mock_sonarr.get_missing_series.return_value = {
            1: "Already",
            2: "Partial Show",
        }
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: mock_sonarr,
        )

        calls: list[tuple] = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda c, i, matched_nzb, **kw: calls.append((i["dl_id"], i["arr_id"])),
        )

        trigger_pending_searches(conn, secret_key="test-key")

        # One call from the main pass, one from the partial-missing pass.
        assert calls == [("sonarr:Already", 1), ("sonarr:Partial Show", 2)]

    def test_sonarr_partial_missing_skipped_when_client_missing(self, monkeypatch):
        conn = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.fetch_arr_queue",
            lambda c, _sk: [],
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )
        calls = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda *a, **kw: calls.append(a),
        )

        trigger_pending_searches(conn, secret_key="test-key")

        assert calls == []


class TestAbandonEndpoint:
    """POST /api/downloads/{dl_id}/abandon"""

    def _make_client(self, db_path, secret_key):
        from mediaman.db import init_db

        conn = init_db(str(db_path))
        app = _make_downloads_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)
        return client

    def test_movie_happy_path(self, db_path, secret_key, monkeypatch):
        """POST with no seasons on a movie item → 200, abandon_movie called once."""
        called = {}

        def fake_abandon_movie(conn, sk, *, arr_id, dl_id):
            called["arr_id"] = arr_id
            called["dl_id"] = dl_id
            from mediaman.services.downloads.abandon import AbandonResult

            return AbandonResult(kind="movie", succeeded=[0], dl_id=dl_id)

        monkeypatch.setattr("mediaman.web.routes.downloads.abandon_movie", fake_abandon_movie)
        monkeypatch.setattr(
            "mediaman.web.routes.downloads.build_downloads_response",
            lambda c, sk: {"queue": [], "hero": None, "upcoming": [], "recent": []},
        )
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: {"kind": "movie", "arr_id": 42, "dl_id": dl_id},
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/radarr%3ATenet/abandon",
            json={},
        )
        assert resp.status_code == 200
        assert called == {"arr_id": 42, "dl_id": "radarr:Tenet"}
        body = resp.json()
        assert body["ok"] is True
        assert body["abandoned"]["kind"] == "movie"

    def test_series_happy_path(self, db_path, secret_key, monkeypatch):
        """POST with seasons on a series item → 200, abandon_seasons called with correct numbers."""
        called = {}

        def fake_abandon_seasons(conn, sk, *, series_id, season_numbers, dl_id):
            called["series_id"] = series_id
            called["season_numbers"] = season_numbers
            called["dl_id"] = dl_id
            from mediaman.services.downloads.abandon import AbandonResult

            return AbandonResult(kind="series", succeeded=season_numbers, dl_id=dl_id)

        monkeypatch.setattr("mediaman.web.routes.downloads.abandon_seasons", fake_abandon_seasons)
        monkeypatch.setattr(
            "mediaman.web.routes.downloads.build_downloads_response",
            lambda c, sk: {"queue": [], "hero": None, "upcoming": [], "recent": []},
        )
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: {"kind": "series", "arr_id": 7, "dl_id": dl_id},
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/sonarr%3ASeverance/abandon",
            json={"seasons": [21, 22]},
        )
        assert resp.status_code == 200
        assert called["series_id"] == 7
        assert called["season_numbers"] == [21, 22]
        body = resp.json()
        assert body["ok"] is True
        assert body["abandoned"]["kind"] == "series"

    def test_upcoming_series_dispatches_to_abandon_series(self, db_path, secret_key, monkeypatch):
        """Upcoming series in the "Coming soon" list → abandon_series, no seasons body needed."""
        called = {}

        def fake_abandon_series(conn, sk, *, series_id, dl_id):
            called["series_id"] = series_id
            called["dl_id"] = dl_id
            from mediaman.services.downloads.abandon import AbandonResult

            return AbandonResult(kind="series", succeeded=[1, 2], dl_id=dl_id)

        monkeypatch.setattr("mediaman.web.routes.downloads.abandon_series", fake_abandon_series)
        monkeypatch.setattr(
            "mediaman.web.routes.downloads.build_downloads_response",
            lambda c, sk: {"queue": [], "hero": None, "upcoming": [], "recent": []},
        )
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: {
                "kind": "series",
                "arr_id": 7,
                "state": "upcoming",
                "dl_id": dl_id,
            },
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/sonarr%3AFutureShow/abandon",
            json={},  # no seasons body
        )
        assert resp.status_code == 200
        assert called == {"series_id": 7, "dl_id": "sonarr:FutureShow"}
        body = resp.json()
        assert body["ok"] is True
        assert body["abandoned"]["kind"] == "series"

    def test_unknown_dl_id_returns_404(self, db_path, secret_key, monkeypatch):
        """POST for a dl_id not in the queue → 404."""
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: None,
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/radarr%3ADoesNotExist/abandon",
            json={},
        )
        assert resp.status_code == 404

    def test_empty_seasons_on_series_returns_400(self, db_path, secret_key, monkeypatch):
        """POST with empty seasons list on a series item → 400."""
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: {"kind": "series", "arr_id": 7, "dl_id": dl_id},
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/sonarr%3ASeverance/abandon",
            json={"seasons": []},
        )
        assert resp.status_code == 400

    def test_unauthenticated_request_is_rejected(self, db_path, secret_key):
        """POST without a session cookie → 401 or 403."""
        from mediaman.db import init_db

        conn = init_db(str(db_path))
        app = _make_downloads_app(conn, secret_key)
        client = TestClient(app)  # no cookie set

        resp = client.post(
            "/api/downloads/radarr%3ATenet/abandon",
            json={},
        )
        assert resp.status_code in (401, 403)

    def test_lookup_uses_real_payload_id_field(self, db_path, secret_key, monkeypatch):
        """Integration: _lookup_dl_item must find items via the canonical 'id'
        field produced by build_item, not the missing 'dl_id' key.

        Does NOT monkey-patch _lookup_dl_item — verifies the whole stack from
        POST through lookup, payload key matching, and abandon dispatch.
        """
        called = {}

        def fake_abandon_movie(conn, sk, *, arr_id, dl_id):
            called["arr_id"] = arr_id
            called["dl_id"] = dl_id
            from mediaman.services.downloads.abandon import AbandonResult

            return AbandonResult(kind="movie", succeeded=[0], dl_id=dl_id)

        monkeypatch.setattr("mediaman.web.routes.downloads.abandon_movie", fake_abandon_movie)

        # A searching movie item — matches what fetch_arr_queue returns for a
        # monitored Radarr title that has no NZBGet match.
        searching_item = {
            "kind": "movie",
            "dl_id": "radarr:Tenet",
            "title": "Tenet",
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

        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.fetch_arr_queue",
            lambda c, _sk: [searching_item],
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db",
            lambda c, _sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.maybe_trigger_search",
            lambda *a, **kw: None,
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/radarr%3ATenet/abandon",
            json={},
        )

        assert resp.status_code == 200, (
            f"Expected 200 but got {resp.status_code} — "
            "likely _lookup_dl_item is still comparing against 'dl_id' instead of 'id'"
        )
        assert called.get("arr_id") == 42
        assert called.get("dl_id") == "radarr:Tenet"
