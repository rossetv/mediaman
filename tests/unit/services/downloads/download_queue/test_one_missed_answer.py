"""The top card stays on its item through one answer its service misses."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from mediaman.services.downloads.download_queue import (
    _reset_previous_queue,
    build_downloads_response,
)


def _queued(title: str, done: int) -> dict[str, object]:
    return {
        "title": f"{title}.2021.1080p",
        "size": 100,
        "sizeleft": 100 - done,
        "status": "downloading",
        "movie": {"title": title},
        "series": {"id": 7, "title": title},
        "episode": {"seasonNumber": 1, "episodeNumber": 1, "title": "One"},
    }


@pytest.fixture
def services(monkeypatch):
    """A Radarr downloading a film and a Sonarr downloading a show, further along than it."""
    _reset_previous_queue()
    radarr, sonarr = MagicMock(), MagicMock()
    radarr.get_queue.return_value = [_queued("Dune", 30)]
    sonarr.get_queue.return_value = [_queued("Severance", 80)]
    radarr.get_movies.return_value = []
    sonarr.get_series.return_value = []
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: radarr)
    monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: sonarr)
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.build_nzbget_from_db", lambda *a: None
    )
    yield radarr, sonarr
    _reset_previous_queue()


def test_the_film_on_top_stays_there_when_its_service_misses_one_answer(conn, services):
    radarr, _ = services

    def look() -> tuple[str, list[str], list[str]]:
        data = build_downloads_response(conn, "k" * 64)
        assert data["hero"] is not None
        return data["hero"]["id"], [i["id"] for i in data["queue"]], data["unreachable"]

    first = look()
    radarr.get_queue.side_effect = requests.ConnectionError("down")
    missed = look()
    radarr.get_queue.side_effect = None
    back = look()

    assert first[0].startswith("radarr:")
    assert missed == back == first


def test_what_acts_on_an_item_is_never_shown_a_missed_answer_as_given(conn, services):
    radarr, _ = services
    build_downloads_response(conn, "k" * 64)
    radarr.get_queue.side_effect = requests.ConnectionError("down")
    data = build_downloads_response(conn, "k" * 64, steady=False)
    assert data["unreachable"] == ["Radarr"]
    assert "radarr:" not in str([data["hero"], *data["queue"]])


def test_a_missed_answer_filled_by_the_last_starts_no_search(conn, services, monkeypatch):
    radarr, _ = services
    searched: list[str] = []
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.maybe_trigger_search",
        lambda _conn, item, **_kw: searched.append(item["dl_id"]),
    )
    build_downloads_response(conn, "k" * 64)
    asked = list(searched)
    radarr.get_queue.side_effect = requests.ConnectionError("down")
    data = build_downloads_response(conn, "k" * 64)
    assert data["unreachable"] == []
    assert any(dl_id.startswith("radarr:") for dl_id in asked)
    assert searched[len(asked) :] == [d for d in asked if not d.startswith("radarr:")]
