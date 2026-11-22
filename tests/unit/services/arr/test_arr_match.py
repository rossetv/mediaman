"""Tests for matching Plex items to Radarr/Sonarr entries."""

from __future__ import annotations

from mediaman.services.arr.match import (
    build_arr_index,
    index_match_movie,
    index_match_season,
    verify_movie,
    verify_season,
)

FILM = "/data/movies/Dune (2021)/Dune (2021) Bluray-1080p.mkv"


def _movie(**overrides):
    movie = {
        "id": 5,
        "tmdbId": 438631,
        "path": "/movies/Dune (2021)",
        "movieFile": {"relativePath": "Dune (2021) Bluray-1080p.mkv", "size": 8_000},
    }
    movie.update(overrides)
    return movie


def _series(**overrides):
    series = {
        "id": 9,
        "tvdbId": 81189,
        "path": "/tv/Breaking Bad",
        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
    }
    series.update(overrides)
    return series


S1_TRACKED = [
    {"path": "/data/tv/Breaking Bad/Season 01/S01E01.mkv", "size": 100},
    {"path": "/data/tv/Breaking Bad/Season 01/S01E02.mkv", "size": 200},
]
S1_FILES = [
    {"id": 1, "seasonNumber": 1, "relativePath": "Season 01/S01E01.mkv", "size": 100},
    {"id": 2, "seasonNumber": 1, "relativePath": "Season 01/S01E02.mkv", "size": 200},
    {"id": 3, "seasonNumber": 2, "relativePath": "Season 02/S02E01.mkv", "size": 300},
]


class TestIndex:
    def test_movie_matches_on_tmdb_and_folder_name(self):
        index = build_arr_index([_movie()], [])
        assert index_match_movie(index, tmdb_id=438631, plex_file_path=FILM) == 5

    def test_movie_in_a_different_folder_is_not_matched(self):
        index = build_arr_index([_movie(path="/movies/Dune Part One (2021)")], [])
        assert index_match_movie(index, tmdb_id=438631, plex_file_path=FILM) is None

    def test_tmdb_on_two_entries_is_dropped_as_ambiguous(self):
        index = build_arr_index([_movie(), _movie(id=6, path="/movies-4k/Dune (2021)")], [])
        assert index_match_movie(index, tmdb_id=438631, plex_file_path=FILM) is None

    def test_tvdb_on_two_entries_is_dropped_as_ambiguous(self):
        index = build_arr_index([], [_series(), _series(id=10)])
        assert (
            index_match_season(
                index, tvdb_id=81189, season_number=1, plex_file_path="/tv/Breaking Bad/Season 01"
            )
            is None
        )

    def test_entries_without_an_id_are_skipped(self):
        index = build_arr_index([_movie(id=None), _movie(tmdbId=0)], [_series(tvdbId=None)])
        assert index.movies == {} and index.series == {}

    def test_season_matches_through_its_season_folder(self):
        index = build_arr_index([], [_series()])
        assert (
            index_match_season(
                index, tvdb_id=81189, season_number=2, plex_file_path="/data/Breaking Bad/Season 02"
            )
            == 9
        )

    def test_season_matches_a_show_without_season_folders(self):
        index = build_arr_index([], [_series()])
        assert (
            index_match_season(
                index, tvdb_id=81189, season_number=1, plex_file_path="/data/Breaking Bad"
            )
            == 9
        )

    def test_season_sonarr_does_not_have_is_not_matched(self):
        index = build_arr_index([], [_series()])
        assert (
            index_match_season(
                index, tvdb_id=81189, season_number=3, plex_file_path="/tv/Breaking Bad/Season 03"
            )
            is None
        )

    def test_season_in_another_show_folder_is_not_matched(self):
        index = build_arr_index([], [_series()])
        assert (
            index_match_season(
                index, tvdb_id=81189, season_number=1, plex_file_path="/tv/El Camino/Season 01"
            )
            is None
        )


class TestVerifyMovie:
    TRACKED = [{"path": FILM, "size": 8_000}]

    def test_same_id_folder_name_and_size_passes(self):
        assert verify_movie(_movie(), tmdb_id=438631, tracked_files=self.TRACKED).ok

    def test_second_library_copy_with_another_size_is_refused(self):
        # A 4K library entry: same TMDB id, same folder name, same file name, other size.
        four_k = _movie(
            path="/movies-4k/Dune (2021)",
            movieFile={"relativePath": "Dune (2021) Bluray-1080p.mkv", "size": 60_000},
        )
        result = verify_movie(four_k, tmdb_id=438631, tracked_files=self.TRACKED)
        assert not result.ok
        assert "different file" in result.reason

    def test_other_tmdb_id_is_refused(self):
        assert not verify_movie(_movie(), tmdb_id=1, tracked_files=self.TRACKED).ok

    def test_other_folder_name_is_refused(self):
        entry = _movie(path="/movies/Dune")
        result = verify_movie(entry, tmdb_id=438631, tracked_files=self.TRACKED)
        assert not result.ok
        assert "folder" in result.reason

    def test_folder_name_differing_only_in_case_is_refused(self):
        entry = _movie(path="/movies/dune (2021)")
        assert not verify_movie(entry, tmdb_id=438631, tracked_files=self.TRACKED).ok

    def test_missing_movie_file_is_refused(self):
        entry = _movie()
        del entry["movieFile"]
        assert not verify_movie(entry, tmdb_id=438631, tracked_files=self.TRACKED).ok

    def test_empty_or_missing_tracked_files_are_refused(self):
        assert not verify_movie(_movie(), tmdb_id=438631, tracked_files=[]).ok
        assert not verify_movie(_movie(), tmdb_id=438631, tracked_files=None).ok

    def test_file_matching_in_another_folder_is_refused(self):
        tracked = [
            {"path": "/data/movies/Dune (2021)/poster.jpg", "size": 1},
            {"path": "/data/other/Dune (2021) Bluray-1080p.mkv", "size": 8_000},
        ]
        assert not verify_movie(_movie(), tmdb_id=438631, tracked_files=tracked).ok


class TestVerifySeason:
    def _verify(self, series=None, files=S1_FILES, tracked=S1_TRACKED, season=1):
        return verify_season(
            series or _series(),
            files,
            tvdb_id=81189,
            season_number=season,
            tracked_files=tracked,
        )

    def test_every_episode_file_tracked_passes(self):
        assert self._verify().ok

    def test_untracked_episode_file_in_the_season_is_refused(self):
        extra = [*S1_FILES, {"seasonNumber": 1, "relativePath": "Season 01/S01E03.mkv", "size": 5}]
        result = self._verify(files=extra)
        assert not result.ok
        assert "does not show" in result.reason

    def test_episode_file_with_another_size_is_refused(self):
        files = [dict(S1_FILES[0], size=101), S1_FILES[1]]
        assert not self._verify(files=files).ok

    def test_season_without_episode_files_is_refused(self):
        assert not self._verify(files=[S1_FILES[2]]).ok

    def test_other_tvdb_id_is_refused(self):
        assert not self._verify(series=_series(tvdbId=1)).ok

    def test_other_series_folder_is_refused(self):
        assert not self._verify(series=_series(path="/tv/Breaking Bad (2008)")).ok

    def test_empty_tracked_files_are_refused(self):
        assert not self._verify(tracked=[]).ok

    def test_a_tracked_episode_sonarr_does_not_manage_is_refused(self):
        tracked = [*S1_TRACKED, {"path": "/data/tv/Breaking Bad/Season 01/S01E03.mkv", "size": 5}]
        result = self._verify(tracked=tracked)
        assert not result.ok
        assert "Sonarr does not manage" in result.reason

    def test_a_tracked_optimized_version_is_refused(self):
        tracked = [
            *S1_TRACKED,
            {"path": "/data/tv/Breaking Bad/Season 01/Plex Versions/S01E01.mp4", "size": 50},
        ]
        assert not self._verify(tracked=tracked).ok

    def test_a_copy_of_the_season_in_another_library_root_is_refused(self):
        copy = [
            {"path": "/data/tv4k/Breaking Bad/Season 01/S01E01.mkv", "size": 100},
            {"path": "/data/tv4k/Breaking Bad/Season 01/S01E02.mkv", "size": 200},
        ]
        assert not self._verify(tracked=[*S1_TRACKED, *copy]).ok
