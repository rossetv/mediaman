"""Tests for the downloads queue-building logic.

Covers:
- build_downloads_response bucket routing (hero/queue/upcoming/recent)
- NZB series matching (dedup, substring theft, punctuation normalisation)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from mediaman.services.arr.search_trigger import reset_search_triggers
from mediaman.services.downloads.download_queue import (
    _reset_previous_queue,
)
from mediaman.services.downloads.download_queue import (
    build_downloads_response as _build_downloads_response,
)


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
