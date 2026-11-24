"""TMDB/TVDB ids and tracked files read from Plex items."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mediaman.services.media_meta._plex_types import _movie_to_item, _season_to_item


def _part(path: str, size: int) -> SimpleNamespace:
    return SimpleNamespace(file=path, size=size)


def _media(*parts: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(parts=list(parts))


def _guid(gid: str) -> SimpleNamespace:
    return SimpleNamespace(id=gid)


def _movie(guid: str, guids=(), media=()) -> SimpleNamespace:
    return SimpleNamespace(
        ratingKey=1,
        title="Film",
        addedAt=datetime(2024, 1, 1, tzinfo=UTC),
        updatedAt=None,
        thumb="/thumb",
        guid=guid,
        guids=list(guids),
        media=list(media),
    )


def _show(guid: str, guids=()) -> SimpleNamespace:
    return SimpleNamespace(
        ratingKey=10,
        title="Show",
        updatedAt=None,
        thumb="/thumb",
        guid=guid,
        guids=list(guids),
        genres=[],
        studio=None,
        librarySectionTitle="TV Shows",
    )


def _season(episodes) -> SimpleNamespace:
    return SimpleNamespace(ratingKey=11, index=1, addedAt=None, episodes=lambda: list(episodes))


def _episode(*media: SimpleNamespace, key=21, number=1) -> SimpleNamespace:
    return SimpleNamespace(ratingKey=key, index=number, media=list(media), addedAt=None)


class TestMovieTmdbId:
    @pytest.mark.parametrize(
        ("guid", "guids", "expected"),
        [
            ("plex://movie/5d77", ["imdb://tt0133093", "tmdb://603", "tvdb://169"], 603),
            ("com.plexapp.agents.themoviedb://603?lang=en", [], 603),
            ("com.plexapp.agents.imdb://tt0133093?lang=en", [], None),
            ("local://1", [], None),
            ("plex://movie/5d77", ["imdb://tt0133093"], None),
        ],
        ids=["new-agent", "legacy-tmdb", "legacy-imdb", "unmatched", "new-agent-no-tmdb"],
    )
    def test_tmdb_id_from_guids(self, guid, guids, expected):
        item = _movie(guid, guids=[_guid(g) for g in guids])
        assert _movie_to_item(item)["tmdb_id"] == expected

    def test_guids_not_read_for_an_item_without_a_new_agent_guid(self):
        """plexapi reloads a partial item when an empty ``guids`` is read."""

        class Unmatched(SimpleNamespace):
            @property
            def guids(self):
                raise AssertionError("guids read")

        item = Unmatched(**{k: v for k, v in vars(_movie("local://1")).items() if k != "guids"})
        assert _movie_to_item(item)["tmdb_id"] is None


class TestSeasonTvdbId:
    @pytest.mark.parametrize(
        ("guid", "guids", "expected"),
        [
            ("plex://show/5d9c", ["tmdb://1399", "tvdb://121361"], 121361),
            ("com.plexapp.agents.thetvdb://121361?lang=en", [], 121361),
            ("com.plexapp.agents.themoviedb://1399?lang=en", [], None),
        ],
        ids=["new-agent", "legacy-tvdb", "legacy-tmdb-show"],
    )
    def test_tvdb_id_from_show_guids(self, guid, guids, expected):
        show = _show(guid, guids=[_guid(g) for g in guids])
        assert _season_to_item(show, _season([]))["tvdb_id"] == expected


class TestTrackedFiles:
    def test_multi_part_movie_tracks_every_part(self):
        movie = _movie(
            "plex://movie/1",
            media=[_media(_part("/m/Film/cd1.mkv", 100), _part("/m/Film/cd2.mkv", 200))],
        )
        item = _movie_to_item(movie)
        assert item["tracked_files"] == [
            {"path": "/m/Film/cd1.mkv", "size": 100},
            {"path": "/m/Film/cd2.mkv", "size": 200},
        ]
        assert item["file_path"] == "/m/Film/cd1.mkv"
        assert item["file_size_bytes"] == 300

    def test_movie_without_media_tracks_nothing(self):
        assert _movie_to_item(_movie("plex://movie/1"))["tracked_files"] == []

    def test_season_tracks_every_episode_file_once(self):
        double = _part("/tv/Show/Season 01/S01E01-E02.mkv", 700)
        episodes = [
            _episode(_media(double)),
            _episode(_media(double)),
            _episode(_media(_part("/tv/Show/Season 01/S01E03.mkv", 300))),
        ]
        item = _season_to_item(_show("plex://show/1"), _season(episodes))
        assert item["tracked_files"] == [
            {"path": "/tv/Show/Season 01/S01E01-E02.mkv", "size": 700},
            {"path": "/tv/Show/Season 01/S01E03.mkv", "size": 300},
        ]
        assert item["file_path"] == "/tv/Show/Season 01"

    def test_season_names_the_number_and_file_of_each_episode(self):
        versions = [_media(_part("/tv/b.mkv", 1)), _media(_part("/tv/a.mkv", 1))]
        episodes = [
            _episode(*versions, key=21, number=4),
            _episode(*reversed(versions), key=22, number=None),
            _episode(key=23, number=6),
        ]
        item = _season_to_item(_show("plex://show/1"), _season(episodes))
        assert item["episodes"] == [
            {"key": "21", "number": 4, "path": "/tv/a.mkv"},
            {"key": "22", "number": None, "path": "/tv/a.mkv"},
            {"key": "23", "number": 6, "path": ""},
        ]
