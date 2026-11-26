"""A download that came in but that Radarr or Sonarr will not import without a person: how its
queue records read, what the Downloads page and the strip make of it, and what a viewer is told."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from mediaman.services.arr.fetcher._radarr import fetch_radarr_queue
from mediaman.services.arr.fetcher._sonarr import fetch_sonarr_queue
from mediaman.services.downloads.download_format import (
    attention_reason,
    map_arr_status,
    map_queue_state,
)
from mediaman.services.downloads.download_queue.items import build_unmatched_arr_item
from mediaman.services.downloads.strip import Answer, Strip, collect, payload

_NOW = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
_WHY = "Single episode file contains all episodes in seasons. Review file name or manually import"


def _sonarr_record(episode: int, **over: object) -> dict[str, object]:
    """One of Sonarr's queue records of a season pack it cannot map to its episodes."""
    return {
        "id": 900 + episode,
        "seriesId": 7,
        "episodeId": 70 + episode,
        "title": "Alone.S12.1080p.WEB-DL",
        "series": {"id": 7, "title": "Alone", "tmdbId": 63726, "images": []},
        "episode": {"id": 70 + episode, "seasonNumber": 12, "episodeNumber": episode},
        "size": 1_000,
        "sizeleft": 0,
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importPending",
        "statusMessages": [{"title": "Alone.S12.1080p.WEB-DL", "messages": [_WHY]}],
        "errorMessage": "Unpack: SUCCESS, Move: SUCCESS",
        "downloadId": "SABnzbd_nzo_1",
    } | over


def _radarr_record(**over: object) -> dict[str, object]:
    return {
        "id": 31,
        "movieId": 5,
        "title": "Ember.Coast.2024.1080p",
        "movie": {"id": 5, "title": "Ember Coast", "tmdbId": 4242, "images": []},
        "size": 1_000,
        "sizeleft": 0,
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importBlocked",
        "statusMessages": [{"title": "Ember.Coast.mkv", "messages": ["Not an upgrade"]}],
        "downloadId": "nzb-31",
    } | over


def _client(queue: list[dict[str, object]]) -> MagicMock:
    client = MagicMock()
    client.get_queue.return_value = queue
    return client


@pytest.fixture(autouse=True)
def _no_searches():
    with patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(0, 0.0)):
        yield


def _downloads_item(card):
    return build_unmatched_arr_item(card, {}, lambda *a, **k: "", lambda *a: "")


class TestClassifier:
    @pytest.mark.parametrize("tracked", ["importPending", "importBlocked", "failedPending"])
    def test_a_warning_on_a_download_that_came_in_needs_attention(self, tracked):
        assert map_arr_status("completed", tracked, "warning") == "needs_attention"

    def test_an_error_on_a_failed_download_needs_attention(self):
        assert map_arr_status("failed", "failed", "error") == "needs_attention"

    def test_a_healthy_import_is_still_almost_ready(self):
        assert map_arr_status("completed", "importPending", "ok") == "almost_ready"

    def test_a_warning_while_it_downloads_leaves_it_downloading(self):
        assert map_arr_status("downloading", "downloading", "warning") == "downloading"

    def test_a_show_with_an_episode_still_downloading_reads_as_it_would_with_no_warning(self):
        held = {
            "status": "completed",
            "tracked_state": "importPending",
            "tracked_status": "warning",
        }
        coming = {"status": "downloading", "tracked_state": "downloading", "tracked_status": "ok"}
        assert map_queue_state([held, coming]) == map_queue_state(
            [{**held, "tracked_status": "ok"}, coming]
        )
        assert map_queue_state([held, held]) == "needs_attention"


class TestReason:
    def test_the_first_status_message_is_the_reason(self):
        assert attention_reason(_sonarr_record(1)) == _WHY

    def test_the_error_message_stands_in_only_where_there_is_no_status_message(self):
        assert attention_reason(_sonarr_record(1, statusMessages=[])) == (
            "Unpack: SUCCESS, Move: SUCCESS"
        )

    def test_a_status_message_title_that_is_the_release_name_is_no_reason(self):
        record = _sonarr_record(1, statusMessages=[{"title": "Alone.S12.1080p.WEB-DL"}])
        assert attention_reason({**record, "errorMessage": ""}) == ""

    def test_a_long_reason_is_cut_to_one_short_line(self):
        long = "word " * 200
        reason = attention_reason(_sonarr_record(1, statusMessages=[{"messages": [long]}]))
        assert len(reason) <= 160
        assert reason.endswith("…")
        assert "  " not in reason


class TestDownloadsPage:
    def test_a_show_sonarr_cannot_import_is_one_card_that_needs_attention(self):
        cards = fetch_sonarr_queue(_client([_sonarr_record(n) for n in (1, 2, 3)]))
        assert len(cards) == 1
        item = _downloads_item(cards[0])
        assert item["state"] == "needs_attention"
        assert item["state_label"] == "Needs attention"
        assert item["attention"] == _WHY
        assert item["attention_hint"] == "Fix it in Sonarr: Activity → Queue"
        assert item["eta"] == ""

    def test_a_show_keeps_the_ids_its_queue_records_are_known_by(self):
        (card,) = fetch_sonarr_queue(_client([_sonarr_record(1)]))
        (episode,) = card["episodes"]
        assert card["series_id"] == 7
        assert (episode["queue_id"], episode["episode_id"]) == (901, 71)
        assert episode["download_id"] == "SABnzbd_nzo_1"

    def test_a_film_radarr_cannot_import_needs_attention(self):
        (card,) = fetch_radarr_queue(_client([_radarr_record()]))
        assert (card["queue_id"], card["movie_id"], card["download_id"]) == (31, 5, "nzb-31")
        item = _downloads_item(card)
        assert item["state"] == "needs_attention"
        assert item["attention"] == "Not an upgrade"
        assert item["attention_hint"] == "Fix it in Radarr: Activity → Queue"

    def test_a_healthy_import_is_almost_ready_with_nothing_to_fix(self):
        healthy = _sonarr_record(1, trackedDownloadStatus="ok", statusMessages=[])
        item = _downloads_item(fetch_sonarr_queue(_client([healthy]))[0])
        assert item["state"] == "almost_ready"
        assert (item["attention"], item["attention_hint"]) == ("", "")


class TestStrip:
    def _shown(self, *, admin: bool) -> dict[str, object]:
        item = _downloads_item(fetch_sonarr_queue(_client([_sonarr_record(1)]))[0])
        data = {
            "hero": None,
            "queue": [item],
            "upcoming": [],
            "recent": [],
            "subtitle": "",
            "unreachable": [],
            "refused": [],
            "outage": "",
            "downloads_set_up": {},
        }
        strip = Strip(tuple(collect(data, (), _NOW)))
        (shown,) = payload(Answer(strip, 0.0), admin=admin)["items"]
        return shown

    def test_an_admin_is_told_what_is_wrong_and_where_to_fix_it(self):
        shown = self._shown(admin=True)
        assert shown["state"] == "needs_attention"
        assert shown["percent"] == 0
        assert shown["attention"] == _WHY
        assert shown["attention_hint"] == "Fix it in Sonarr: Activity → Queue"

    def test_a_viewer_is_told_only_that_it_needs_attention(self):
        shown = self._shown(admin=False)
        assert shown["state"] == "needs_attention"
        assert (shown["attention"], shown["attention_hint"]) == ("", "")
