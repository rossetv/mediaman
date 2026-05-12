"""Tests for fetch_arr_queue enrichment logic.

Covers :func:`mediaman.services.arr.fetcher.fetch_arr_queue`:
- upcoming movies are included regardless of added date
- stalled/released movies are included regardless of added date
- arr_id and release_label are propagated correctly
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mediaman.services.arr.fetcher import fetch_arr_queue as _get_arr_queue


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
