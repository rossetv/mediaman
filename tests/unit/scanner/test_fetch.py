"""Tests for mediaman.scanner.fetch.

Covers PlexFetcher.fetch_library_items for both movie and show libraries,
including watch-history error tolerance.
"""

from unittest.mock import MagicMock

from mediaman.scanner.fetch import PlexFetcher


def _make_movie(rk="101", title="Test Film"):
    return {"plex_rating_key": rk, "title": title, "added_at": "2024-01-01T00:00:00Z"}


def _make_season(rk="201", is_anime=False):
    return {"plex_rating_key": rk, "title": "Season 1", "is_anime": is_anime}


def _make_plex(*, movies=None, seasons=None, watch_history=None, season_history=None):
    client = MagicMock()
    client.get_movie_items.return_value = movies or []
    client.get_show_seasons.return_value = seasons or []
    client.get_watch_history.return_value = watch_history or []
    client.get_season_watch_history.return_value = season_history or []
    return client


# ---------------------------------------------------------------------------
# movie libraries
# ---------------------------------------------------------------------------


class TestFetchMovieLibrary:
    def test_returns_one_record_per_movie(self):
        plex = _make_plex(movies=[_make_movie("1"), _make_movie("2")])
        fetcher = PlexFetcher(plex_client=plex, library_types={"10": "movie"})
        results = fetcher.fetch_library_items("10")
        assert len(results) == 2

    def test_media_type_is_movie(self):
        plex = _make_plex(movies=[_make_movie()])
        fetcher = PlexFetcher(plex_client=plex, library_types={"10": "movie"})
        results = fetcher.fetch_library_items("10")
        assert results[0].media_type == "movie"

    def test_watch_history_attached(self):
        history = [{"viewed_at": "2024-06-01T00:00:00Z"}]
        plex = _make_plex(movies=[_make_movie()], watch_history=history)
        fetcher = PlexFetcher(plex_client=plex, library_types={"10": "movie"})
        results = fetcher.fetch_library_items("10")
        assert results[0].watch_history == history

    def test_watch_history_error_gives_empty_list(self):
        plex = _make_plex(movies=[_make_movie()])
        plex.get_watch_history.side_effect = RuntimeError("Plex unavailable")
        fetcher = PlexFetcher(plex_client=plex, library_types={"10": "movie"})
        results = fetcher.fetch_library_items("10")
        assert results[0].watch_history == []

    def test_empty_library_returns_empty_list(self):
        plex = _make_plex(movies=[])
        fetcher = PlexFetcher(plex_client=plex, library_types={"10": "movie"})
        assert fetcher.fetch_library_items("10") == []

    def test_unknown_library_defaults_to_movie(self):
        plex = _make_plex(movies=[_make_movie()])
        fetcher = PlexFetcher(plex_client=plex, library_types={})
        results = fetcher.fetch_library_items("99")
        assert results[0].media_type == "movie"


# ---------------------------------------------------------------------------
# show libraries
# ---------------------------------------------------------------------------


class TestFetchShowLibrary:
    def test_returns_one_record_per_season(self):
        seasons = [_make_season("201"), _make_season("202")]
        plex = _make_plex(seasons=seasons)
        fetcher = PlexFetcher(plex_client=plex, library_types={"20": "show"})
        results = fetcher.fetch_library_items("20")
        assert len(results) == 2

    def test_media_type_is_tv_season_by_default(self):
        plex = _make_plex(seasons=[_make_season()])
        fetcher = PlexFetcher(plex_client=plex, library_types={"20": "show"})
        results = fetcher.fetch_library_items("20")
        assert results[0].media_type == "tv_season"

    def test_anime_flag_on_season_overrides_media_type(self):
        plex = _make_plex(seasons=[_make_season(is_anime=True)])
        fetcher = PlexFetcher(plex_client=plex, library_types={"20": "show"})
        results = fetcher.fetch_library_items("20")
        assert results[0].media_type == "anime_season"

    def test_anime_library_title_sets_default_anime(self):
        # Library title contains "anime" — seasons without is_anime set explicitly
        # inherit the default and are classified as anime_season.
        season_without_flag = {"plex_rating_key": "201", "title": "Season 1"}  # no is_anime key
        plex = _make_plex(seasons=[season_without_flag])
        fetcher = PlexFetcher(
            plex_client=plex,
            library_types={"20": "show"},
            library_titles={"20": "anime collection"},  # lowercase; "anime" in title
        )
        results = fetcher.fetch_library_items("20")
        assert results[0].media_type == "anime_season"

    def test_season_watch_history_error_gives_empty_list(self):
        plex = _make_plex(seasons=[_make_season()])
        plex.get_season_watch_history.side_effect = RuntimeError("timeout")
        fetcher = PlexFetcher(plex_client=plex, library_types={"20": "show"})
        results = fetcher.fetch_library_items("20")
        assert results[0].watch_history == []

    def test_library_id_passed_through(self):
        plex = _make_plex(seasons=[_make_season()])
        fetcher = PlexFetcher(plex_client=plex, library_types={"20": "show"})
        results = fetcher.fetch_library_items("20")
        assert results[0].library_id == "20"
