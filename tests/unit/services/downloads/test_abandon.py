"""Tests for the abandon-search service."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.services.arr.search_trigger import (
    _save_trigger_to_db,
    reset_search_triggers,
)
from mediaman.services.downloads.abandon import (
    AbandonResult,
    abandon_movie,
    abandon_seasons,
    abandon_series,
)


@pytest.fixture(autouse=True)
def clean_state():
    reset_search_triggers()
    yield
    reset_search_triggers()


@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(str(tmp_path / "mediaman.db"))
    yield conn
    conn.close()


class TestAbandonMovie:
    def test_calls_unmonitor_and_clears_throttle(self, db_conn, monkeypatch):
        """Happy path: unmonitor_movie called once, throttle row deleted."""
        client = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_radarr_from_db",
            lambda c, sk: client,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: None,
        )
        _save_trigger_to_db(db_conn, "radarr:Tenet", 999.0, 5)

        result = abandon_movie(db_conn, "secret", arr_id=42, dl_id="radarr:Tenet")

        client.unmonitor_movie.assert_called_once_with(42)
        assert result == AbandonResult(kind="movie", succeeded=[0], failed=[], dl_id="radarr:Tenet")
        from mediaman.services.arr.search_trigger import _load_throttle_from_db

        assert _load_throttle_from_db(db_conn, "radarr:Tenet") == (0.0, 0)

    def test_returns_partial_failure_when_unmonitor_raises(self, db_conn, monkeypatch):
        """Sonarr/Radarr down: surfaced as failed list; throttle untouched."""
        client = MagicMock()
        client.unmonitor_movie.side_effect = RuntimeError("radarr down")
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_radarr_from_db",
            lambda c, sk: client,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: None,
        )
        _save_trigger_to_db(db_conn, "radarr:Tenet", 999.0, 5)

        result = abandon_movie(db_conn, "secret", arr_id=42, dl_id="radarr:Tenet")

        assert result.failed == [0]
        assert result.succeeded == []
        from mediaman.services.arr.search_trigger import _load_throttle_from_db

        _, count = _load_throttle_from_db(db_conn, "radarr:Tenet")
        assert count == 5  # preserved on failure

    def test_no_radarr_client_returns_failure(self, db_conn, monkeypatch):
        """The builder returning None is treated as a failure, not silent."""
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: None,
        )
        result = abandon_movie(db_conn, "secret", arr_id=42, dl_id="radarr:Tenet")
        assert result.failed == [0]
        assert result.succeeded == []


class TestAbandonSeasons:
    def test_loops_per_season_and_clears_throttle_on_full_success(self, db_conn, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: client,
        )
        _save_trigger_to_db(db_conn, "sonarr:One Piece", 999.0, 47)

        result = abandon_seasons(
            db_conn,
            "secret",
            series_id=7,
            season_numbers=[21, 22],
            dl_id="sonarr:One Piece",
        )

        assert client.unmonitor_season.call_args_list == [
            ((7, 21),),
            ((7, 22),),
        ]
        assert result == AbandonResult(
            kind="series", succeeded=[21, 22], failed=[], dl_id="sonarr:One Piece"
        )
        from mediaman.services.arr.search_trigger import _load_throttle_from_db

        assert _load_throttle_from_db(db_conn, "sonarr:One Piece") == (0.0, 0)

    def test_partial_failure_records_both_lists_and_keeps_throttle(self, db_conn, monkeypatch):
        client = MagicMock()

        def fail_22(series_id, season_number):
            if season_number == 22:
                raise RuntimeError("sonarr blew up on season 22")

        client.unmonitor_season.side_effect = fail_22
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: client,
        )
        _save_trigger_to_db(db_conn, "sonarr:One Piece", 999.0, 47)

        result = abandon_seasons(
            db_conn,
            "secret",
            series_id=7,
            season_numbers=[21, 22],
            dl_id="sonarr:One Piece",
        )

        assert sorted(result.succeeded) == [21]
        assert sorted(result.failed) == [22]
        from mediaman.services.arr.search_trigger import _load_throttle_from_db

        _, count = _load_throttle_from_db(db_conn, "sonarr:One Piece")
        assert count == 47

    def test_empty_season_list_raises_value_error(self, db_conn, monkeypatch):
        """An empty list is rejected — nothing to abandon."""
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: MagicMock(),
        )
        with pytest.raises(ValueError, match="at least one season"):
            abandon_seasons(
                db_conn,
                "secret",
                series_id=7,
                season_numbers=[],
                dl_id="sonarr:X",
            )


class TestAbandonSeries:
    def test_unmonitors_every_monitored_season(self, db_conn, monkeypatch):
        """Coming-soon series → all monitored seasons get unmonitored."""
        client = MagicMock()
        client.get_series_by_id.return_value = {
            "id": 7,
            "seasons": [
                {"seasonNumber": 0, "monitored": False},
                {"seasonNumber": 1, "monitored": True},
                {"seasonNumber": 2, "monitored": True},
            ],
        }
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: client,
        )

        result = abandon_series(
            db_conn,
            "secret",
            series_id=7,
            dl_id="sonarr:Future Show",
        )

        client.get_series_by_id.assert_called_once_with(7)
        assert client.unmonitor_season.call_args_list == [((7, 1),), ((7, 2),)]
        assert sorted(result.succeeded) == [1, 2]
        assert result.failed == []

    def test_no_monitored_seasons_is_a_noop_success(self, db_conn, monkeypatch):
        """Series already fully unmonitored → success, no calls made."""
        client = MagicMock()
        client.get_series_by_id.return_value = {
            "id": 7,
            "seasons": [{"seasonNumber": 1, "monitored": False}],
        }
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: client,
        )

        result = abandon_series(db_conn, "secret", series_id=7, dl_id="sonarr:Done")

        client.unmonitor_season.assert_not_called()
        assert result.succeeded == [] and result.failed == []

    def test_get_series_failure_returns_failed(self, db_conn, monkeypatch):
        client = MagicMock()
        client.get_series_by_id.side_effect = RuntimeError("sonarr down")
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.build_sonarr_from_db",
            lambda c, sk: client,
        )

        result = abandon_series(db_conn, "secret", series_id=7, dl_id="sonarr:Boom")

        assert result.failed == [0]
        client.unmonitor_season.assert_not_called()
