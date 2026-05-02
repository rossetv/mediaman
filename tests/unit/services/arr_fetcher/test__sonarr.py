"""Tests for mediaman.services.arr.fetcher._sonarr."""

from __future__ import annotations

from unittest.mock import MagicMock

from mediaman.services.arr.fetcher._sonarr import (
    _aggregate_pack_episodes,
    _make_sonarr_card,
    fetch_sonarr_queue,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(queue=None, series=None, episodes=None):
    c = MagicMock()
    c.get_queue.return_value = queue or []
    c.get_series.return_value = series or []
    c.get_episodes.return_value = episodes or []
    return c


def _series_payload(series_id=1, title="Breaking Bad", year=2008):
    return {"id": series_id, "title": title, "year": year, "images": []}


def _queue_ep(
    series=None,
    ep_season=1,
    ep_num=1,
    ep_title="Pilot",
    size=500_000_000,
    sizeleft=0,
    status="completed",
    download_id="dl-001",
):
    return {
        "title": f"Breaking.Bad.S{ep_season:02d}E{ep_num:02d}",
        "series": series or _series_payload(),
        "episode": {"seasonNumber": ep_season, "episodeNumber": ep_num, "title": ep_title},
        "size": size,
        "sizeleft": sizeleft,
        "status": status,
        "downloadId": download_id,
    }


# ---------------------------------------------------------------------------
# _make_sonarr_card
# ---------------------------------------------------------------------------


class TestMakeSonarrCard:
    def test_kind_is_series(self):
        card = _make_sonarr_card("Breaking Bad")
        assert card["kind"] == "series"

    def test_source_is_sonarr(self):
        card = _make_sonarr_card("Breaking Bad")
        assert card["source"] == "Sonarr"

    def test_dl_id_contains_title(self):
        card = _make_sonarr_card("Breaking Bad")
        assert "Breaking Bad" in card["dl_id"]

    def test_episodes_defaults_to_empty_list(self):
        card = _make_sonarr_card("Chernobyl")
        assert card["episodes"] == []

    def test_release_names_defaults_to_empty_list(self):
        card = _make_sonarr_card("Silo")
        assert card["release_names"] == []


# ---------------------------------------------------------------------------
# _aggregate_pack_episodes
# ---------------------------------------------------------------------------


class TestAggregatePackEpisodes:
    def _card_with_eps(self, eps):
        card = _make_sonarr_card("Test Show", episodes=eps)
        return card

    def test_individual_episodes_not_flagged_as_pack(self):
        eps = [
            {
                "label": "S01E01",
                "title": "Ep 1",
                "progress": 100,
                "size": 500_000_000,
                "sizeleft": 0,
                "download_id": "dl-001",
            },
            {
                "label": "S01E02",
                "title": "Ep 2",
                "progress": 100,
                "size": 600_000_000,
                "sizeleft": 0,
                "download_id": "dl-002",
            },
        ]
        card = self._card_with_eps(eps)
        _aggregate_pack_episodes(card, card_series_id=1)
        assert all(e["is_pack_episode"] is False for e in card["episodes"])
        assert card["size"] == 1_100_000_000
        assert card["episode_count"] == 2

    def test_pack_episodes_flagged_and_deduplicated(self):
        """Two episodes sharing a downloadId → both flagged pack; size counted once."""
        pack_ep = {
            "label": "S01E01",
            "title": "Ep 1",
            "progress": 100,
            "size": 2_000_000_000,
            "sizeleft": 0,
            "download_id": "pack-abc",
        }
        pack_ep2 = {
            "label": "S01E02",
            "title": "Ep 2",
            "progress": 100,
            "size": 2_000_000_000,
            "sizeleft": 0,
            "download_id": "pack-abc",
        }
        card = self._card_with_eps([pack_ep, pack_ep2])
        _aggregate_pack_episodes(card, card_series_id=1)
        assert all(e["is_pack_episode"] is True for e in card["episodes"])
        assert card["size"] == 2_000_000_000  # counted once

    def test_empty_download_id_distinct_keys(self):
        """Empty downloadIds are disambiguated by title+label so no double-counting."""
        eps = [
            {
                "label": "S01E01",
                "title": "Ep 1",
                "progress": 100,
                "size": 400_000_000,
                "sizeleft": 0,
                "download_id": "",
            },
            {
                "label": "S01E02",
                "title": "Ep 2",
                "progress": 100,
                "size": 500_000_000,
                "sizeleft": 0,
                "download_id": "",
            },
        ]
        card = self._card_with_eps(eps)
        _aggregate_pack_episodes(card, card_series_id=7)
        assert all(e["is_pack_episode"] is False for e in card["episodes"])
        assert card["size"] == 900_000_000

    def test_episodes_sorted_by_label(self):
        eps = [
            {
                "label": "S01E03",
                "title": "C",
                "progress": 0,
                "size": 0,
                "sizeleft": 0,
                "download_id": "x3",
            },
            {
                "label": "S01E01",
                "title": "A",
                "progress": 0,
                "size": 0,
                "sizeleft": 0,
                "download_id": "x1",
            },
        ]
        card = self._card_with_eps(eps)
        _aggregate_pack_episodes(card, card_series_id=1)
        assert [e["label"] for e in card["episodes"]] == ["S01E01", "S01E03"]


# ---------------------------------------------------------------------------
# fetch_sonarr_queue — queue items
# ---------------------------------------------------------------------------


class TestFetchSonarrQueueItems:
    def test_single_episode_becomes_card(self):
        client = _client(queue=[_queue_ep()])
        cards = fetch_sonarr_queue(client)
        assert len(cards) == 1
        assert cards[0]["title"] == "Breaking Bad"
        assert cards[0]["kind"] == "series"

    def test_episodes_from_same_series_merged(self):
        """Two episodes of the same series → one card with two episodes."""
        sp = _series_payload()
        client = _client(
            queue=[
                _queue_ep(series=sp, ep_num=1, download_id="d1"),
                _queue_ep(series=sp, ep_num=2, download_id="d2"),
            ]
        )
        cards = fetch_sonarr_queue(client)
        assert len(cards) == 1
        assert cards[0]["episode_count"] == 2

    def test_release_name_recorded(self):
        ep = _queue_ep()
        ep["title"] = "Breaking.Bad.S01E01.720p"
        client = _client(queue=[ep])
        cards = fetch_sonarr_queue(client)
        assert "Breaking.Bad.S01E01.720p" in cards[0]["release_names"]

    def test_empty_queue_returns_empty(self):
        client = _client(queue=[], series=[])
        assert fetch_sonarr_queue(client) == []


# ---------------------------------------------------------------------------
# fetch_sonarr_queue — searching series
# ---------------------------------------------------------------------------


class TestFetchSonarrQueueSearching:
    def test_monitored_zero_files_appears(self):
        series = [
            {
                "id": 10,
                "title": "House of the Dragon",
                "year": 2022,
                "monitored": True,
                "statistics": {"episodeFileCount": 0},
                "images": [],
                "added": "2024-01-01T00:00:00Z",
                "titleSlug": "house-of-the-dragon",
            }
        ]
        client = _client(queue=[], series=series, episodes=[])
        cards = fetch_sonarr_queue(client)
        assert any(c["title"] == "House of the Dragon" for c in cards)

    def test_unmonitored_series_excluded(self):
        series = [
            {
                "id": 11,
                "title": "Cancelled Show",
                "year": 2019,
                "monitored": False,
                "statistics": {"episodeFileCount": 0},
                "images": [],
                "added": "2024-01-01T00:00:00Z",
            }
        ]
        client = _client(queue=[], series=series)
        cards = fetch_sonarr_queue(client)
        assert not any(c["title"] == "Cancelled Show" for c in cards)

    def test_series_with_files_excluded(self):
        series = [
            {
                "id": 12,
                "title": "Already Got It",
                "year": 2020,
                "monitored": True,
                "statistics": {"episodeFileCount": 5},
                "images": [],
                "added": "2024-01-01T00:00:00Z",
            }
        ]
        client = _client(queue=[], series=series)
        cards = fetch_sonarr_queue(client)
        assert not any(c["title"] == "Already Got It" for c in cards)

    def test_get_series_exception_does_not_crash(self):
        """Network error from get_series is swallowed; queue cards returned."""
        import requests

        client = MagicMock()
        client.get_queue.return_value = [_queue_ep()]
        client.get_series.side_effect = requests.RequestException("down")
        cards = fetch_sonarr_queue(client)
        assert any(c["title"] == "Breaking Bad" for c in cards)

    def test_get_series_safehttp_503_does_not_crash(self):
        """A 503 from Sonarr (raised as ``SafeHTTPError``) is caught.

        Regression for finding Domain-06 #2: ``SafeHTTPClient`` raises
        ``SafeHTTPError`` (NOT a ``RequestException`` subclass) for
        non-2xx responses; the previous code dropped every already-
        collected card when get_series returned 503.
        """
        from mediaman.services.infra.http_client import SafeHTTPError

        client = MagicMock()
        client.get_queue.return_value = [_queue_ep()]
        client.get_series.side_effect = SafeHTTPError(503, "down", "/api/v3/series")
        cards = fetch_sonarr_queue(client)
        assert any(c["title"] == "Breaking Bad" for c in cards)

    def test_get_episodes_safehttp_503_recovers(self):
        """A 503 from get_episodes is caught; the searching card still appears.

        Regression for finding Domain-06 #2 (inner case): ``get_episodes``
        sits inside the still-searching loop and previously only caught
        ``RequestException`` — a 503 propagated to the outer handler and
        wiped the partial result.
        """
        from mediaman.services.infra.http_client import SafeHTTPError

        client = MagicMock()
        client.get_queue.return_value = []
        client.get_series.return_value = [
            {
                "id": 99,
                "title": "Andor",
                "year": 2022,
                "monitored": True,
                "statistics": {"episodeFileCount": 0},
                "images": [],
                "added": "2024-01-01T00:00:00Z",
            }
        ]
        client.get_episodes.side_effect = SafeHTTPError(503, "down", "/api/v3/episode")
        cards = fetch_sonarr_queue(client)
        assert any(c["title"] == "Andor" for c in cards)


# ---------------------------------------------------------------------------
# fetch_sonarr_queue — season_number on episode entries
# ---------------------------------------------------------------------------


class TestSeasonNumberOnEntries:
    def test_season_number_populated_from_sonarr_payload(self):
        """Each ArrEpisodeEntry built from the queue carries season_number."""
        client = _client(
            queue=[
                {
                    "series": {"id": 7, "title": "One Piece", "year": 1999, "images": []},
                    "episode": {"seasonNumber": 21, "episodeNumber": 3, "title": "x"},
                    "size": 100,
                    "sizeleft": 50,
                    "status": "downloading",
                    "downloadId": "abc",
                    "title": "release.title",
                }
            ]
        )

        cards = fetch_sonarr_queue(client)

        assert len(cards) == 1
        episodes = cards[0].get("episodes") or []
        assert len(episodes) == 1
        assert episodes[0]["season_number"] == 21

    def test_season_number_defaults_to_zero_when_missing(self):
        """Missing seasonNumber in the payload yields season_number=0."""
        client = _client(
            queue=[
                {
                    "series": {"id": 8, "title": "Mystery Show", "year": 2020, "images": []},
                    "episode": {"episodeNumber": 1, "title": "Pilot"},
                    "size": 200,
                    "sizeleft": 0,
                    "status": "completed",
                    "downloadId": "xyz",
                    "title": "Mystery.Show.E01",
                }
            ]
        )

        cards = fetch_sonarr_queue(client)

        episodes = cards[0].get("episodes") or []
        assert len(episodes) == 1
        assert episodes[0]["season_number"] == 0
