"""Tests for mediaman.services.arr.fetcher._radarr."""

from __future__ import annotations

from unittest.mock import MagicMock

from mediaman.services.arr.fetcher._radarr import _make_radarr_card, fetch_radarr_queue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(queue: list[dict] | None = None, movies: list[dict] | None = None) -> MagicMock:
    """Build a minimal Radarr client stub."""
    c = MagicMock()
    c.get_queue.return_value = queue or []
    c.get_movies.return_value = movies or []
    return c


def _queue_item(
    title: str = "Dune",
    status: str = "downloading",
    size: int = 4_000_000_000,
    sizeleft: int = 2_000_000_000,
    timeleft: str = "01:00:00",
    movie_title: str | None = None,
    movie_year: int | None = 2021,
) -> dict:
    return {
        "title": title,
        "status": status,
        "size": size,
        "sizeleft": sizeleft,
        "timeleft": timeleft,
        "movie": {
            "title": movie_title or title,
            "year": movie_year,
            "images": [],
        },
    }


# ---------------------------------------------------------------------------
# _make_radarr_card
# ---------------------------------------------------------------------------


class TestMakeRadarrCard:
    def test_kind_is_movie(self):
        card = _make_radarr_card("Dune")
        assert card["kind"] == "movie"

    def test_source_is_radarr(self):
        card = _make_radarr_card("Dune")
        assert card["source"] == "Radarr"

    def test_dl_id_contains_title(self):
        card = _make_radarr_card("Dune")
        assert "Dune" in card["dl_id"]

    def test_size_strings_populated(self):
        card = _make_radarr_card("Oppenheimer", size=4_000_000_000, sizeleft=2_000_000_000)
        assert card["size_str"]
        assert card["done_str"]

    def test_release_names_defaults_to_empty_list(self):
        card = _make_radarr_card("Dune")
        assert card["release_names"] == []

    def test_is_upcoming_defaults_false(self):
        card = _make_radarr_card("Dune")
        assert card["is_upcoming"] is False


# ---------------------------------------------------------------------------
# fetch_radarr_queue — queue items
# ---------------------------------------------------------------------------


class TestFetchRadarrQueueItems:
    def test_queue_item_becomes_card(self):
        client = _client(queue=[_queue_item("Dune")])
        cards = fetch_radarr_queue(client)
        assert len(cards) == 1
        assert cards[0]["title"] == "Dune"
        assert cards[0]["kind"] == "movie"

    def test_progress_calculated_correctly(self):
        """50% downloaded → progress == 50."""
        client = _client(queue=[_queue_item(size=1_000, sizeleft=500)])
        cards = fetch_radarr_queue(client)
        assert cards[0]["progress"] == 50

    def test_progress_zero_when_size_is_zero(self):
        """If size is 0 we must not divide by zero; progress should be 0."""
        item = _queue_item()
        item["size"] = 0
        item["sizeleft"] = 0
        client = _client(queue=[item])
        cards = fetch_radarr_queue(client)
        assert cards[0]["progress"] == 0

    def test_release_name_recorded(self):
        client = _client(queue=[_queue_item("Dune.2021.1080p.BluRay")])
        cards = fetch_radarr_queue(client)
        assert "Dune.2021.1080p.BluRay" in cards[0]["release_names"]

    def test_empty_queue_returns_empty(self):
        client = _client(queue=[], movies=[])
        assert fetch_radarr_queue(client) == []


# ---------------------------------------------------------------------------
# fetch_radarr_queue — library (searching) items
# ---------------------------------------------------------------------------


class TestFetchRadarrQueueSearching:
    def test_monitored_no_file_appears_as_searching(self):
        movies = [
            {
                "id": 1,
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
        client = _client(queue=[], movies=movies)
        cards = fetch_radarr_queue(client)
        assert any(c["title"] == "Oppenheimer" for c in cards)

    def test_unmonitored_movie_excluded(self):
        movies = [
            {
                "id": 2,
                "title": "Hidden",
                "year": 2020,
                "monitored": False,
                "hasFile": False,
                "isAvailable": True,
                "images": [],
                "added": "2024-01-01T00:00:00Z",
            }
        ]
        client = _client(queue=[], movies=movies)
        cards = fetch_radarr_queue(client)
        assert not any(c["title"] == "Hidden" for c in cards)

    def test_movie_with_file_excluded(self):
        movies = [
            {
                "id": 3,
                "title": "AlreadyDownloaded",
                "year": 2020,
                "monitored": True,
                "hasFile": True,
                "isAvailable": True,
                "images": [],
                "added": "2024-01-01T00:00:00Z",
            }
        ]
        client = _client(queue=[], movies=movies)
        cards = fetch_radarr_queue(client)
        assert not any(c["title"] == "AlreadyDownloaded" for c in cards)

    def test_movie_already_in_queue_not_duplicated(self):
        """A movie present in the queue must not also appear as a searching card."""
        movies = [
            {
                "id": 4,
                "title": "Dune",
                "year": 2021,
                "monitored": True,
                "hasFile": False,
                "isAvailable": True,
                "images": [],
                "added": "2024-01-01T00:00:00Z",
            }
        ]
        client = _client(
            queue=[_queue_item("Dune", movie_title="Dune", movie_year=2021)],
            movies=movies,
        )
        cards = fetch_radarr_queue(client)
        dune_cards = [c for c in cards if c["title"] == "Dune"]
        assert len(dune_cards) == 1

    def test_get_movies_exception_does_not_crash(self):
        """If get_movies raises a requests error, queue items still returned."""
        import requests

        client = MagicMock()
        client.get_queue.return_value = [_queue_item("Dune")]
        client.get_movies.side_effect = requests.RequestException("timeout")
        cards = fetch_radarr_queue(client)
        assert len(cards) >= 1
        assert cards[0]["title"] == "Dune"
