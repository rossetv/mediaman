"""Tests for fetch_arr_queue."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.services.arr_fetcher import fetch_arr_queue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn(tmp_path) -> sqlite3.Connection:
    return init_db(str(tmp_path / "mediaman.db"))


def _put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, '2026-01-01')",
        (key, value),
    )
    conn.commit()


def _configure_radarr(conn: sqlite3.Connection) -> None:
    _put(conn, "radarr_url", "http://radarr.local")
    _put(conn, "radarr_api_key", "test-key")


def _configure_sonarr(conn: sqlite3.Connection) -> None:
    _put(conn, "sonarr_url", "http://sonarr.local")
    _put(conn, "sonarr_api_key", "test-key")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_config():
    """Provide a minimal Config so load_config() doesn't raise in tests.

    fetch_arr_queue does ``from mediaman.config import load_config`` inside
    the function body, so we must patch at the source module.
    """
    fake_config = MagicMock()
    fake_config.secret_key = "test-secret-32-chars-XXXXXXXXXX"
    with patch("mediaman.config.load_config", return_value=fake_config):
        yield fake_config


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = _make_conn(tmp_path)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Tests: empty / unconfigured
# ---------------------------------------------------------------------------

def test_returns_empty_when_nothing_configured(conn):
    result = fetch_arr_queue(conn)
    assert result == []


@patch("mediaman.services.radarr.RadarrClient")
def test_returns_empty_when_radarr_not_configured(mock_radarr_cls, conn):
    """No Radarr credentials → no cards, RadarrClient never constructed."""
    result = fetch_arr_queue(conn)
    mock_radarr_cls.assert_not_called()
    assert result == []


# ---------------------------------------------------------------------------
# Tests: Radarr queue
# ---------------------------------------------------------------------------

@patch("mediaman.services.radarr.RadarrClient")
def test_radarr_queue_movie_appears(mock_radarr_cls, conn, tmp_path):
    _configure_radarr(conn)

    mock_client = MagicMock()
    mock_client.get_queue.return_value = [
        {
            "title": "Dune.2021.1080p",
            "size": 5_000_000_000,
            "sizeleft": 2_500_000_000,
            "status": "downloading",
            "timeleft": "00:30:00",
            "movie": {
                "title": "Dune",
                "year": 2021,
                "images": [],
            },
        }
    ]
    mock_client.get_movies.return_value = []
    mock_radarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)

    assert len(result) == 1
    card = result[0]
    assert card["kind"] == "movie"
    assert card["title"] == "Dune"
    assert card["source"] == "Radarr"
    assert card["progress"] == 50


@patch("mediaman.services.radarr.RadarrClient")
def test_radarr_searching_movie_appears(mock_radarr_cls, conn):
    """Monitored movies with no file that are not in the queue show up as 'searching'."""
    _configure_radarr(conn)

    mock_client = MagicMock()
    mock_client.get_queue.return_value = []
    mock_client.get_movies.return_value = [
        {
            "id": 7,
            "title": "Oppenheimer",
            "year": 2023,
            "monitored": True,
            "hasFile": False,
            "isAvailable": True,
            "images": [],
            "added": "2024-01-01T00:00:00Z",
            "titleSlug": "oppenheimer-2023",
        }
    ]
    mock_radarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)

    assert any(c["title"] == "Oppenheimer" and c["status"] == "searching" for c in result)


@patch("mediaman.services.radarr.RadarrClient")
def test_is_upcoming_flag_set_for_future_release(mock_radarr_cls, conn):
    """A movie with a future physicalRelease date and no file gets is_upcoming=True."""
    _configure_radarr(conn)

    mock_client = MagicMock()
    mock_client.get_queue.return_value = []
    mock_client.get_movies.return_value = [
        {
            "id": 99,
            "title": "Future Film",
            "year": 2099,
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "physicalRelease": "2099-12-25T00:00:00Z",
            "images": [],
            "added": "2026-01-01T00:00:00Z",
            "titleSlug": "future-film",
        }
    ]
    mock_radarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)

    upcoming = [c for c in result if c["title"] == "Future Film"]
    assert len(upcoming) == 1
    assert upcoming[0]["is_upcoming"] is True


@patch("mediaman.services.radarr.RadarrClient")
def test_unmonitored_movie_excluded(mock_radarr_cls, conn):
    """Unmonitored movies must not appear in the queue."""
    _configure_radarr(conn)

    mock_client = MagicMock()
    mock_client.get_queue.return_value = []
    mock_client.get_movies.return_value = [
        {
            "id": 1,
            "title": "Hidden Film",
            "year": 2020,
            "monitored": False,
            "hasFile": False,
            "isAvailable": True,
            "images": [],
            "added": "2024-01-01T00:00:00Z",
        }
    ]
    mock_radarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)
    assert not any(c["title"] == "Hidden Film" for c in result)


@patch("mediaman.services.sonarr.SonarrClient")
@patch("mediaman.services.radarr.RadarrClient")
def test_radarr_failure_does_not_crash(mock_radarr_cls, mock_sonarr_cls, conn):
    """Exception from RadarrClient is swallowed; Sonarr items still returned."""
    _configure_radarr(conn)
    _configure_sonarr(conn)

    # Radarr raises on construction
    mock_radarr_cls.side_effect = RuntimeError("connection refused")

    sonarr_client = MagicMock()
    sonarr_client.get_queue.return_value = [
        {
            "title": "Breaking.Bad.S01E01",
            "size": 1_000_000_000,
            "sizeleft": 0,
            "status": "completed",
            "series": {"id": 1, "title": "Breaking Bad", "year": 2008, "images": []},
            "episode": {"seasonNumber": 1, "episodeNumber": 1, "title": "Pilot"},
            "downloadId": "abc123",
        }
    ]
    sonarr_client.get_series.return_value = []
    mock_sonarr_cls.return_value = sonarr_client

    result = fetch_arr_queue(conn)

    # Sonarr card present; no exception raised
    assert any(c["kind"] == "series" and c["title"] == "Breaking Bad" for c in result)


# ---------------------------------------------------------------------------
# Tests: Sonarr queue
# ---------------------------------------------------------------------------

@patch("mediaman.services.sonarr.SonarrClient")
def test_sonarr_series_appear_in_queue(mock_sonarr_cls, conn):
    _configure_sonarr(conn)

    mock_client = MagicMock()
    mock_client.get_queue.return_value = [
        {
            "title": "The.Wire.S01E01",
            "size": 2_000_000_000,
            "sizeleft": 1_000_000_000,
            "status": "downloading",
            "series": {
                "id": 10,
                "title": "The Wire",
                "year": 2002,
                "images": [],
            },
            "episode": {
                "seasonNumber": 1,
                "episodeNumber": 1,
                "title": "The Target",
            },
            "downloadId": "dl-001",
        }
    ]
    mock_client.get_series.return_value = []
    mock_sonarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)

    assert len(result) == 1
    card = result[0]
    assert card["kind"] == "series"
    assert card["title"] == "The Wire"
    assert card["source"] == "Sonarr"
    assert len(card["episodes"]) == 1


@patch("mediaman.services.sonarr.SonarrClient")
def test_sonarr_episodes_grouped_by_series(mock_sonarr_cls, conn):
    """Multiple queue items for the same series are merged into one card."""
    _configure_sonarr(conn)

    series_payload = {"id": 5, "title": "Chernobyl", "year": 2019, "images": []}
    mock_client = MagicMock()
    mock_client.get_queue.return_value = [
        {
            "title": "Chernobyl.S01E01",
            "size": 500_000_000,
            "sizeleft": 0,
            "status": "completed",
            "series": series_payload,
            "episode": {"seasonNumber": 1, "episodeNumber": 1, "title": "1:23:45"},
            "downloadId": "x1",
        },
        {
            "title": "Chernobyl.S01E02",
            "size": 500_000_000,
            "sizeleft": 0,
            "status": "completed",
            "series": series_payload,
            "episode": {"seasonNumber": 1, "episodeNumber": 2, "title": "Please Remain Calm"},
            "downloadId": "x2",
        },
    ]
    mock_client.get_series.return_value = []
    mock_sonarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)

    assert len(result) == 1
    assert result[0]["episode_count"] == 2


@patch("mediaman.services.sonarr.SonarrClient")
def test_sonarr_empty_downloadids_do_not_double_count(mock_sonarr_cls, conn):
    """C19 — two queue rows with empty downloadId must not collapse into one pack total."""
    _configure_sonarr(conn)

    series_payload = {"id": 7, "title": "Orphan Black", "year": 2013, "images": []}
    mock_client = MagicMock()
    mock_client.get_queue.return_value = [
        {
            "title": "Orphan.Black.S01E01",
            "size": 400_000_000,
            "sizeleft": 0,
            "status": "completed",
            "series": series_payload,
            "episode": {"seasonNumber": 1, "episodeNumber": 1, "title": "Natural Selection"},
            "downloadId": "",  # empty — dangerous pre-fix
        },
        {
            "title": "Orphan.Black.S01E02",
            "size": 500_000_000,
            "sizeleft": 0,
            "status": "completed",
            "series": series_payload,
            "episode": {"seasonNumber": 1, "episodeNumber": 2, "title": "Instinct"},
            "downloadId": "",  # empty — dangerous pre-fix
        },
    ]
    mock_client.get_series.return_value = []
    mock_sonarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)

    assert len(result) == 1
    card = result[0]
    assert card["episode_count"] == 2
    # With the fix, both episodes' sizes contribute (distinct cluster
    # keys because title/label differ). Pre-fix, they would have shared
    # the "" dl key and one would have been skipped OR flagged as a pack
    # with duplicated aggregate. Either way the check is: both episode
    # sizes add up correctly.
    assert card["size"] == 900_000_000
    # Neither episode should be flagged is_pack_episode because their
    # cluster keys differ (label/title disambiguates).
    assert all(e["is_pack_episode"] is False for e in card["episodes"])


@patch("mediaman.services.sonarr.SonarrClient")
def test_sonarr_pack_with_shared_download_id_still_clusters(mock_sonarr_cls, conn):
    """When a real pack downloadId is shared, we still dedupe correctly."""
    _configure_sonarr(conn)

    series_payload = {"id": 8, "title": "Silo", "year": 2023, "images": []}
    mock_client = MagicMock()
    mock_client.get_queue.return_value = [
        {
            "title": "Silo.S01E01",
            "size": 2_000_000_000,
            "sizeleft": 0,
            "status": "completed",
            "series": series_payload,
            "episode": {"seasonNumber": 1, "episodeNumber": 1, "title": "Freedom Day"},
            "downloadId": "pack-123",
        },
        {
            "title": "Silo.S01E02",
            "size": 2_000_000_000,
            "sizeleft": 0,
            "status": "completed",
            "series": series_payload,
            "episode": {"seasonNumber": 1, "episodeNumber": 2, "title": "Holston's Pick"},
            "downloadId": "pack-123",  # same pack
        },
    ]
    mock_client.get_series.return_value = []
    mock_sonarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)
    assert len(result) == 1
    card = result[0]
    # Pack should dedupe: size counted once.
    assert card["size"] == 2_000_000_000
    assert all(e["is_pack_episode"] is True for e in card["episodes"])


@patch("mediaman.services.sonarr.SonarrClient")
def test_sonarr_searching_series_appears(mock_sonarr_cls, conn):
    """Monitored series with zero episode files but not in the queue show up."""
    _configure_sonarr(conn)

    mock_client = MagicMock()
    mock_client.get_queue.return_value = []
    mock_client.get_series.return_value = [
        {
            "id": 20,
            "title": "House of the Dragon",
            "year": 2022,
            "monitored": True,
            "statistics": {"episodeFileCount": 0},
            "images": [],
            "added": "2024-01-01T00:00:00Z",
            "titleSlug": "house-of-the-dragon",
        }
    ]
    mock_client.get_episodes.return_value = []
    mock_sonarr_cls.return_value = mock_client

    result = fetch_arr_queue(conn)

    assert any(c["title"] == "House of the Dragon" for c in result)
