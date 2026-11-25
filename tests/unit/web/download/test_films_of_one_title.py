"""Two films of one title on the Downloads page: what is done to one is never done to the other."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest
import requests
from fastapi.testclient import TestClient

from mediaman.services.arr.search_trigger import get_search_info, reset_search_triggers
from mediaman.services.downloads.download_queue import _reset_previous_queue
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router

_NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)
_OLD, _NEW = 21, 22


def _film(number: int, **over: object) -> dict[str, object]:
    return {
        "id": number,
        "tmdbId": 1000 + number,
        "title": "Twin",
        "year": 2000 + number,
        "monitored": True,
        "hasFile": False,
        "isAvailable": True,
        "added": "2025-01-01T00:00:00Z",
        "digitalRelease": "2024-01-01T00:00:00Z",
    } | over


def _show(number: int) -> dict[str, object]:
    return {
        "id": number,
        "title": "Twin",
        "monitored": True,
        "added": "2025-01-01T00:00:00Z",
        "statistics": {"episodeFileCount": 0},
        "seasons": [{"seasonNumber": 1, "monitored": True, "statistics": {"episodeCount": 8}}],
    }


class Downloads:
    """A Radarr and a Sonarr, and the Downloads page as an admin asks for it."""

    def __init__(self, client: TestClient, radarr: MagicMock, sonarr: MagicMock) -> None:
        self.client, self.radarr, self.sonarr = client, radarr, sonarr
        self.films: list[dict] = [_film(_OLD), _film(_NEW)]
        radarr.get_movies.side_effect = lambda: self.films
        radarr.get_queue.return_value = []
        radarr.unmonitor_movie.side_effect = lambda n: self.film(n).update(monitored=False)
        sonarr.get_series.return_value = []
        sonarr.get_queue.return_value = []
        sonarr.get_episodes.return_value = [{"airDateUtc": "2020-01-01T00:00:00Z"}]

    def film(self, number: int) -> dict:
        return next(f for f in self.films if f["id"] == number)

    def queue(self, *numbers: int) -> None:
        self.radarr.get_queue.return_value = [
            {"movie": self.film(n), "title": f"Twin.{n}", "size": 100, "sizeleft": 50}
            for n in numbers
        ]

    def page(self) -> dict:
        return self.client.get("/api/downloads").json()

    def ids(self) -> dict[int, str]:
        """Each searching film's id on the page, by its number in Radarr."""
        body = self.page()
        return {i["arr_id"]: i["id"] for i in [body["hero"], *body["queue"]] if i}

    def recent(self) -> list[str]:
        return [i["id"] for i in self.page()["recent"]]

    def abandon(self, dl_id: str, **body: object):
        return self.client.post(f"/api/downloads/{quote(dl_id, safe='')}/abandon", json=body)

    def unmonitored(self) -> list[int]:
        return [call.args[0] for call in self.radarr.unmonitor_movie.call_args_list]


@pytest.fixture
def downloads(app_factory, conn, freezer, monkeypatch):
    freezer.move_to(_NOW)
    reset_search_triggers()
    _reset_previous_queue()
    radarr, sonarr = MagicMock(), MagicMock()
    for module in ("arr.build", "arr.search_trigger", "downloads.abandon"):
        for name, client in (("radarr", radarr), ("sonarr", sonarr)):
            monkeypatch.setattr(
                f"mediaman.services.{module}.build_{name}_from_db",
                lambda *a, client=client: client,
            )
    # Not create_user: hashing a password takes a second.
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role, created_at)"
        " VALUES ('boss', 'x', 'admin', '2026-01-01')"
    )
    conn.commit()
    client = TestClient(app_factory(router, conn=conn))
    client.cookies.set("session_token", create_session(conn, "boss"))
    yield Downloads(client, radarr, sonarr)
    reset_search_triggers()
    _reset_previous_queue()


def _searched(conn, key: str, count: int, at: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO arr_search_throttle VALUES (?, ?, ?)", (key, at.isoformat(), count)
    )
    conn.commit()


def _since(conn, number: int, at: datetime) -> None:
    conn.execute(
        "INSERT INTO arr_searching_since VALUES (?, ?, ?)",
        (f"radarr:{number}", at.isoformat(), _NOW.isoformat()),
    )
    conn.commit()


def _rows(conn) -> dict[str, int]:
    return dict(conn.execute("SELECT key, search_count FROM arr_search_throttle").fetchall())


@pytest.mark.parametrize("number", [_OLD, _NEW])
@pytest.mark.parametrize("over", [{}, {"isAvailable": False}], ids=["searching", "coming soon"])
def test_abandoning_one_film_leaves_the_other_monitored(downloads, number, over):
    for film in downloads.films:
        film.update(over)
    body = downloads.page()
    items = [i for i in [body["hero"], *body["queue"], *body["upcoming"]] if i]
    dl_id = next(i["id"] for i in items if i["arr_id"] == number)
    assert downloads.abandon(dl_id, arr_id=number).status_code == 200
    assert downloads.unmonitored() == [number]


def test_a_page_opened_before_the_films_were_told_apart_abandons_neither(downloads):
    assert downloads.abandon("radarr:Twin").status_code == 404
    assert downloads.unmonitored() == []


def test_a_page_left_open_does_not_abandon_the_film_that_took_the_others_place(downloads):
    downloads.films = [_film(_OLD)]
    shown = downloads.ids()[_OLD]
    downloads.films = [_film(_NEW)]
    assert downloads.abandon(shown, arr_id=_OLD).status_code == 404
    assert downloads.unmonitored() == []


def test_a_film_that_has_begun_to_download_is_not_abandoned_as_the_other(downloads):
    shown = downloads.ids()[_OLD]
    downloads.queue(_OLD)
    assert downloads.abandon(shown, arr_id=_OLD).status_code == 404
    assert downloads.abandon(shown).status_code == 400
    assert downloads.unmonitored() == []


def test_a_film_alone_of_its_title_is_abandoned_by_a_page_that_names_no_number(downloads):
    downloads.films = [_film(_OLD)]
    assert downloads.abandon("radarr:Twin").status_code == 200
    assert downloads.unmonitored() == [_OLD]


def test_of_two_shows_of_one_title_the_one_picked_is_abandoned(downloads):
    downloads.sonarr.get_series.return_value = [_show(3), _show(4)]
    answer = downloads.abandon("sonarr:Twin", seasons=[1], arr_id=4)
    assert answer.status_code == 200
    downloads.sonarr.unmonitor_season.assert_called_once_with(4, 1)


def test_of_two_shows_of_one_title_none_is_abandoned_when_none_was_picked(downloads):
    downloads.sonarr.get_series.return_value = [_show(3), _show(4)]
    assert downloads.abandon("sonarr:Twin", seasons=[1]).status_code == 409
    downloads.sonarr.unmonitor_season.assert_not_called()


def test_a_film_keeps_its_id_from_searching_to_downloading(downloads):
    searching = downloads.ids()
    downloads.queue(_OLD, _NEW)
    body = downloads.page()
    assert {body["hero"]["id"], body["queue"][0]["id"]} == set(searching.values())


def test_a_film_is_still_listed_while_one_of_its_title_and_year_downloads(downloads):
    for film in downloads.films:
        film["year"] = 2020
    downloads.queue(_OLD)
    body = downloads.page()
    assert {body["hero"]["arr_id"], body["queue"][0]["arr_id"]} == {0, _NEW}


def test_each_film_is_recorded_as_it_arrives(downloads):
    downloads.queue(_OLD, _NEW)
    body = downloads.page()
    both = {body["hero"]["id"], body["queue"][0]["id"]}
    for left, arrived in (([_OLD], _NEW), ([], _OLD)):
        downloads.film(arrived).update(hasFile=True, movieFile={"dateAdded": _NOW.isoformat()})
        downloads.queue(*left)
        recent = downloads.recent()
        assert len(recent) == 2 - len(left)
    assert set(recent) == both


def test_a_film_that_arrived_stays_listed_while_the_other_is_searched_for(downloads):
    downloads.queue(_OLD)
    downloads.page()
    downloads.film(_OLD).update(hasFile=True, movieFile={"dateAdded": _NOW.isoformat()})
    downloads.queue()
    assert len(downloads.recent()) == 1
    assert len(downloads.recent()) == 1


def test_a_film_in_the_queue_again_leaves_the_recent_ones(downloads):
    downloads.queue(_OLD)
    downloads.page()
    downloads.film(_OLD).update(hasFile=True, movieFile={"dateAdded": _NOW.isoformat()})
    downloads.queue()
    assert len(downloads.recent()) == 1
    downloads.queue(_OLD)
    assert downloads.recent() == []


def test_how_often_they_were_searched_for_outlives_an_upgrade_and_a_rollback(downloads, conn):
    for number in (_OLD, _NEW):
        _since(conn, number, _NOW - timedelta(days=9))
    _searched(conn, "radarr:Twin", 12, _NOW - timedelta(minutes=5))
    ids = downloads.ids()
    assert [get_search_info(ids[n])[0] for n in (_OLD, _NEW)] == [12, 12]
    assert downloads.abandon(ids[_OLD], arr_id=_OLD).status_code == 200
    assert get_search_info(ids[_NEW])[0] == 12
    # The image before this one knows no other key.
    assert _rows(conn) == {"radarr:Twin": 12, ids[_NEW]: 12}


def test_a_film_added_beside_another_does_not_take_over_its_searches(downloads, conn):
    _since(conn, _OLD, _NOW - timedelta(days=9))
    _searched(conn, "radarr:Twin", 12, _NOW - timedelta(minutes=5))
    ids = downloads.ids()
    assert get_search_info(ids[_OLD])[0] == 12
    # Searched for at once, not after the wait the twelfth search of the other began.
    assert get_search_info(ids[_NEW])[0] == 1


def test_a_film_abandoned_and_monitored_again_starts_its_searches_over(downloads, conn, freezer):
    for number in (_OLD, _NEW):
        _since(conn, number, _NOW - timedelta(days=9))
    _searched(conn, "radarr:Twin", 12, _NOW - timedelta(minutes=5))
    ids = downloads.ids()
    downloads.abandon(ids[_OLD], arr_id=_OLD)
    # A search of either keeps Radarr's others back for a while.
    downloads.radarr.search_movie.side_effect = requests.ConnectionError
    freezer.tick(timedelta(minutes=1))
    downloads.page()
    downloads.film(_OLD)["monitored"] = True
    assert get_search_info(downloads.ids()[_OLD])[0] == 0
