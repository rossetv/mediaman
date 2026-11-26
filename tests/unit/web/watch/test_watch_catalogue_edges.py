"""Edges of the catalogue: a viewer's HEAD of the front page, the old title addresses, an odd
Radarr or Sonarr record, and a library search typed out of order or with a year."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.services.watch_catalog import _arr
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import browse_api, pages
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings


def test_a_viewer_may_head_the_front_page(guarded_app, conn, viewer_client):
    app, reached = guarded_app
    client = viewer_client(app, conn)

    assert client.head("/").status_code == 204
    assert reached == [("HEAD", "/", "viewer")]


@pytest.mark.parametrize("path", ["/watch/movie/5", "/watch/show/5"])
def test_an_old_title_address_moves_for_good(app_factory, conn, path):
    insert_admin_user(conn, username="aoife", role="viewer")
    client = TestClient(app_factory(pages.router, conn=conn), follow_redirects=False)
    client.cookies.set("session_token", create_session(conn, "aoife"))

    resp = client.get(path)

    assert resp.status_code == 301
    assert resp.headers["location"].endswith("movie-5" if "movie" in path else "show-5")


class _OddRadarr:
    def get_movies(self):
        raise TypeError("a record that is not a movie")

    def get_series(self):
        raise KeyError("id")

    def get_queue(self):
        return []


def test_an_odd_record_leaves_the_service_unanswered(caplog):
    caches, answered = _arr._radarr(_OddRadarr())
    sonarr = _arr._sonarr(_OddRadarr())

    assert answered is False
    assert caches["radarr_movies"] == {}
    assert sonarr.answered is False
    assert "Radarr unavailable" in caplog.text
    assert "Sonarr unavailable" in caplog.text


@pytest.fixture
def client(app_factory, conn):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    insert_settings(conn, plex_libraries='["1"]')
    insert_admin_user(conn, username="aoife", role="viewer")
    test_client = TestClient(app_factory(browse_api.router, conn=conn))
    test_client.cookies.set("session_token", create_session(conn, "aoife"))
    return test_client


def _title(conn, key: str, title: str, year: int | None = None) -> None:
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        show_title=title,
        media_type="tv_season",
        season_number=1,
        show_rating_key=f"s{key}",
        plex_library_id=1,
    )
    conn.execute("UPDATE media_items SET year = ? WHERE id = ?", (year, key))


def _found(client, q: str) -> list[str]:
    resp = client.get("/api/watch/search", params={"q": q})
    assert resp.status_code == 200
    return [i["title"] for i in resp.json()["items"]]


@pytest.mark.parametrize("typed", ["the harbour", "harbour northern", "northern the harbour"])
def test_every_word_is_found_in_any_order(client, conn, typed):
    _title(conn, "1", "The Northern Harbour")
    _title(conn, "2", "Something Else")

    assert _found(client, typed) == ["The Northern Harbour"]


def test_a_trailing_year_is_not_a_word_of_the_title(client, conn):
    _title(conn, "1", "Kestrel Point")
    _title(conn, "2", "Something Else")

    assert _found(client, "Kestrel Point 2024") == ["Kestrel Point"]


def test_the_titles_of_the_typed_year_come_first(client, conn):
    _title(conn, "1", "Kestrel Point", 2019)
    _title(conn, "2", "Kestrel Point: Origins", 2024)
    _title(conn, "3", "Something Else", 2024)

    assert _found(client, "kestrel point 2024") == ["Kestrel Point: Origins", "Kestrel Point"]
