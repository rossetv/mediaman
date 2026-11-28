"""The state of a card whose show or film NZBGet holds more than one download of."""

from __future__ import annotations

from unittest.mock import MagicMock

from mediaman.services.arr.fetcher import _radarr, _sonarr
from mediaman.services.downloads.download_queue.queue import build_arr_items, parse_nzb_queue

_MB = 1024 * 1024
_MIB_PER_SEC = _MB


def _group(nzb_id: int, name: str, status: str, file_mb: int, remain_mb: int, category: str):
    """One group as NZBGet's listgroups gives it."""
    return {
        "NZBID": nzb_id,
        "NZBName": name,
        "Category": category,
        "Status": status,
        "FileSizeMB": file_mb,
        "RemainingSizeMB": remain_mb,
        "DownloadedSizeMB": file_mb - remain_mb,
        "ActiveDownloads": 4 if status == "DOWNLOADING" else 0,
        "Health": 1000,
        "PostTotalTimeSec": 0,
    }


def _sonarr_record(qid: int, episode: int, release: str, status: str, size: int, sizeleft: int):
    """One record of Sonarr's /api/v3/queue for an episode of season 2 of Slow Horses."""
    return {
        "id": qid,
        "seriesId": 12,
        "episodeId": 200 + episode,
        "seasonNumber": 2,
        "series": {"id": 12, "title": "Slow Horses", "year": 2022, "tmdbId": 95480, "images": []},
        "episode": {"id": 200 + episode, "seasonNumber": 2, "episodeNumber": episode},
        "title": release,
        "size": size,
        "sizeleft": sizeleft,
        "status": status,
        "trackedDownloadStatus": "ok",
        "trackedDownloadState": "downloading",
        "statusMessages": [],
        "downloadId": str(40 + qid),
        "protocol": "usenet",
        "downloadClient": "NZBGet",
    }


def _card_for(fetcher, records):
    client = MagicMock()
    client.get_queue.return_value = records
    (card,) = fetcher._build_queue_cards(client)
    return card


def _build(card, groups):
    items, _ = build_arr_items(
        MagicMock(),
        [card],
        parse_nzb_queue(groups),
        {},
        _MIB_PER_SEC,
        "k",
        MagicMock(),
    )
    (item,) = items
    return item


E01 = "Slow.Horses.S02E01.1080p.WEB.h264-GRP"
E02 = "Slow.Horses.S02E02.1080p.WEB.h264-GRP"
PACK = "Slow.Horses.S02.1080p.WEB.h264-GRP"


def _two_episodes(first_status: str, first_remain: int):
    card = _card_for(
        _sonarr,
        [
            _sonarr_record(1, 1, E01, "downloading", 1500 * _MB, first_remain * _MB),
            _sonarr_record(2, 2, E02, "queued", 1400 * _MB, 1400 * _MB),
        ],
    )
    groups = [
        _group(41, E01, first_status, 1500, first_remain, "Series"),
        _group(42, E02, "QUEUED", 1400, 1400, "Series"),
    ]
    return card, groups


class TestSeriesWithSeveralDownloads:
    def test_a_downloading_episode_beside_a_queued_one_reads_downloading(self):
        item = _build(*_two_episodes("DOWNLOADING", 300))
        assert item["state"] == "downloading"

    def test_the_time_left_covers_every_episode_still_to_come(self):
        item = _build(*_two_episodes("DOWNLOADING", 300))
        # 300 MB of the first and 1400 MB of the second at 1 MiB/s: 1700 s.
        assert item["eta"] == "28 min left"
        assert item["minutes_left"] == 28

    def test_a_downloading_episode_beside_a_queued_season_pack_reads_downloading(self):
        card = _card_for(
            _sonarr,
            [
                _sonarr_record(1, 1, PACK, "queued", 9000 * _MB, 9000 * _MB),
                _sonarr_record(2, 9, "Slow.Horses.S02E09.1080p.WEB-GRP", "downloading", 0, 0),
            ],
        )
        groups = [
            _group(41, PACK, "QUEUED", 9000, 9000, "Series"),
            _group(42, "Slow.Horses.S02E09.1080p.WEB-GRP", "DOWNLOADING", 1500, 600, "Series"),
        ]
        assert _build(card, groups)["state"] == "downloading"

    def test_a_post_processing_episode_beside_a_queued_one_reads_almost_ready(self):
        item = _build(*_two_episodes("UNPACKING", 0))
        assert item["state"] == "almost_ready"

    def test_a_single_queued_episode_still_reads_queued(self):
        card = _card_for(_sonarr, [_sonarr_record(2, 2, E02, "queued", 1400 * _MB, 1400 * _MB)])
        item = _build(card, [_group(42, E02, "QUEUED", 1400, 1400, "Series")])
        assert item["state"] == "queued"
        assert item["eta"] == ""


class TestFilmWithOneDownload:
    def test_a_film_reads_its_one_downloads_state_and_size(self):
        release = "Dune.Part.Two.2024.2160p.WEB-DL-GRP"
        card = _card_for(
            _radarr,
            [
                {
                    "id": 5,
                    "movieId": 77,
                    "movie": {"id": 77, "title": "Dune: Part Two", "year": 2024, "images": []},
                    "title": release,
                    "size": 8000 * _MB,
                    "sizeleft": 6000 * _MB,
                    "status": "downloading",
                    "trackedDownloadStatus": "ok",
                    "trackedDownloadState": "downloading",
                    "statusMessages": [],
                    "downloadId": "51",
                    "protocol": "usenet",
                    "downloadClient": "NZBGet",
                }
            ],
        )
        item = _build(card, [_group(51, release, "DOWNLOADING", 8000, 6000, "Movies")])
        assert item["state"] == "downloading"
        assert item["progress"] == 25
        assert item["eta"] == "1 h 40 min left"
        assert item["minutes_left"] == 100


def _film_record(title: str, release: str, download_id: str, **more):
    return {
        "id": 5,
        "movieId": 77,
        "movie": {"id": 77, "title": title, "year": 2020, "images": []},
        "title": release,
        "size": 8000 * _MB,
        "sizeleft": 6000 * _MB,
        "status": "downloading",
        "trackedDownloadStatus": "ok",
        "trackedDownloadState": "downloading",
        "statusMessages": [],
        "downloadId": download_id,
        **more,
    }


def _items(cards, groups, queue_paused=False):
    items, _ = build_arr_items(
        MagicMock(),
        cards,
        parse_nzb_queue(groups, queue_paused=queue_paused),
        {},
        _MIB_PER_SEC,
        "k",
        MagicMock(),
    )
    return items


def _claimed(card, groups):
    """The names of the NZBGet downloads the card took as its own."""
    parsed = parse_nzb_queue(groups)
    build_arr_items(MagicMock(), [card], parsed, {}, _MIB_PER_SEC, "k", MagicMock())
    return [nzb["dl_id"] for nzb in parsed if nzb["_matched"]]


class TestWhichDownloadIsTheCards:
    def test_a_short_title_never_takes_a_longer_titles_download(self):
        card = _card_for(_radarr, [_film_record("Alone", "Alone.2020.1080p-GRP", "")])
        home = _group(60, "Home.Alone.1990.1080p.BluRay-GRP", "DOWNLOADING", 8000, 6000, "Movies")

        assert _claimed(card, [home]) == []

    def test_the_download_radarr_names_by_id_is_taken_over_one_of_the_same_name(self):
        card = _card_for(_radarr, [_film_record("Alone", "Alone.2020.1080p-GRP", "abc123")])
        other = _group(60, "Alone.2020.1080p-GRP", "QUEUED", 8000, 8000, "Movies")
        own = {
            **_group(61, "Renamed.By.Indexer", "DOWNLOADING", 8000, 6000, "Movies"),
            "Parameters": [{"Name": "drone", "Value": "ABC123"}],
        }

        assert _claimed(card, [other, own]) == ["Renamed.By.Indexer"]

    def test_with_no_id_nzbget_holds_the_exact_release_wins_over_a_longer_title(self):
        card = _card_for(_radarr, [_film_record("Alone", "Alone.2020.1080p-GRP", "999")])
        home = _group(60, "Home.Alone.1990.1080p.BluRay-GRP", "QUEUED", 9000, 9000, "Movies")
        own = _group(61, "Alone.2020.1080p-GRP", "DOWNLOADING", 8000, 6000, "Movies")

        assert _claimed(card, [home, own]) == ["Alone.2020.1080p-GRP"]


class TestPausedInNzbget:
    def _film(self):
        return _card_for(_radarr, [_film_record("Alone", "Alone.2020.1080p-GRP", "61")])

    def test_a_download_paused_by_itself_says_so_and_names_what_to_resume(self):
        (item,) = _items(
            [self._film()], [_group(61, "Alone.2020.1080p-GRP", "PAUSED", 8000, 6000, "Movies")]
        )

        assert (item["state"], item["state_label"], item["eta"]) == (
            "paused",
            "Paused in NZBGet",
            "",
        )
        assert item["paused_ids"] == [61]

    def test_while_all_of_nzbget_is_paused_nothing_offers_its_own_resume(self):
        group = _group(61, "Alone.2020.1080p-GRP", "DOWNLOADING", 8000, 6000, "Movies")

        (item,) = _items([self._film()], [group], queue_paused=True)

        assert (item["state"], item["state_label"]) == ("paused", "NZBGet is paused")
        assert "paused_ids" not in item


def test_a_stalled_download_nzbget_still_holds_reads_as_needing_attention():
    record = _film_record(
        "Alone",
        "Alone.2020.1080p-GRP",
        "61",
        trackedDownloadStatus="warning",
        statusMessages=[{"title": "x", "messages": ["Stalled: no progress for 4 hours"]}],
    )
    card = _card_for(_radarr, [record])

    (item,) = _items(
        [card], [_group(61, "Alone.2020.1080p-GRP", "DOWNLOADING", 8000, 6000, "Movies")]
    )

    assert item["state"] == "needs_attention"
    assert item["attention"] == "Stalled: no progress for 4 hours"
