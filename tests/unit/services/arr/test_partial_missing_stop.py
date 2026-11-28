"""The partial-missing pass: searched by episode where few are missing, stopped after its tries,
and started again once one of the episodes it missed arrives."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.services.arr import search_trigger as st


@pytest.fixture
def conn(tmp_path):
    st.reset_search_triggers()
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()
    st.reset_search_triggers()


@pytest.fixture
def sonarr(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(st, "build_sonarr_from_db", lambda *a: client)
    return client


def _run(conn, sonarr, missing):
    sonarr.get_missing_episodes.return_value = missing
    # Each run as a later day's: the fan-out cap of the run before has passed.
    st.reset_search_triggers()
    st._trigger_sonarr_partial_missing(conn, [], "key")


def _searches(sonarr) -> int:
    return sonarr.send_command.call_count + sonarr.search_series.call_count


def _tried_out(conn) -> None:
    st._save_trigger_to_db(conn, "sonarr:Avatar", 1.0, st._PARTIAL_TRIES)


def test_a_series_missing_a_few_episodes_is_searched_for_them_alone(conn, sonarr):
    _run(conn, sonarr, {7: ("Avatar", [73, 71])})

    sonarr.send_command.assert_called_once_with({"name": "EpisodeSearch", "episodeIds": [71, 73]})
    sonarr.search_series.assert_not_called()


def test_a_series_missing_many_episodes_is_searched_for_whole(conn, sonarr):
    _run(conn, sonarr, {7: ("Avatar", list(range(100, 100 + st._EPISODE_SEARCH_MAX + 1)))})

    sonarr.search_series.assert_called_once_with(7)
    sonarr.send_command.assert_not_called()


def test_a_series_tried_its_last_time_is_no_longer_searched_and_history_says_so_once(conn, sonarr):
    _tried_out(conn)
    _run(conn, sonarr, {7: ("Avatar", [71, 72, 73])})
    _run(conn, sonarr, {7: ("Avatar", [71, 72, 73])})

    assert _searches(sonarr) == 0
    said = conn.execute("SELECT detail FROM audit_log WHERE action='search_stopped'").fetchall()
    assert len(said) == 1
    assert "3 episodes of 'Avatar'" in said[0][0]


def test_a_stopped_series_is_searched_again_once_an_episode_it_missed_arrives(conn, sonarr):
    _tried_out(conn)
    _run(conn, sonarr, {7: ("Avatar", [71, 72, 73])})
    _run(conn, sonarr, {7: ("Avatar", [72, 73, 80])})

    sonarr.send_command.assert_called_once_with(
        {"name": "EpisodeSearch", "episodeIds": [72, 73, 80]}
    )
    assert st._load_throttle_from_db(conn, "sonarr:Avatar")[1] == 1


def test_a_stopped_series_stays_stopped_when_only_more_episodes_go_missing(conn, sonarr):
    _tried_out(conn)
    _run(conn, sonarr, {7: ("Avatar", [71])})
    _run(conn, sonarr, {7: ("Avatar", [71, 80])})

    assert _searches(sonarr) == 0
