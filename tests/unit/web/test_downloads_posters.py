"""The posters of the Downloads page: TMDB's come through mediaman, and no other service's."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from tests.helpers.factories import insert_admin_user

_TMDB = "https://image.tmdb.org/t/p/original"
_THETVDB = "https://artworks.thetvdb.com/banners/posters/1.jpg"


def _item(poster_url: str) -> dict[str, object]:
    return {
        "id": f"radarr:{poster_url[-8:]}",
        "title": "Ember Coast",
        "media_type": "movie",
        "poster_url": poster_url,
        "state": "downloading",
        "state_label": "Downloading",
        "progress": 10,
        "abandon_visible": False,
        "stuck_seasons": [],
        "release_label": "",
    }


_PAYLOAD = {
    "hero": _item(f"{_TMDB}/hero.jpg"),
    "queue": [_item(f"{_TMDB}/row.jpg"), _item(_THETVDB), _item("")],
    "upcoming": [_item(f"{_TMDB}/soon.jpg")],
    "recent": [_item(f"{_TMDB}/done.jpg")],
    "subtitle": "",
    "unreachable": [],
    "refused": [],
    "outage": "",
    "downloads_set_up": {"movies": True, "shows": True},
}


def test_tmdb_posters_are_loaded_through_mediaman_and_no_other_address_is_passed_on(
    app_factory, conn
):
    insert_admin_user(conn, username="person")
    app = app_factory(downloads_router, conn=conn)
    templates = Path(mediaman.__file__).parent / "web/templates"
    app.state.templates = Jinja2Templates(directory=str(templates))
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "person"))
    built = deepcopy(_PAYLOAD)
    with patch("mediaman.web.routes.downloads.build_downloads_response", return_value=built):
        page = client.get("/downloads").text
        api = client.get("/api/downloads").json()

    proxy = "/api/watch/img/tmdb/w342"
    assert api["hero"]["poster_url"] == f"{proxy}/hero.jpg"
    assert [i["poster_url"] for i in api["queue"]] == [f"{proxy}/row.jpg", "", ""]
    assert "thetvdb" not in page
    assert api["upcoming"][0]["poster_url"] == f"{proxy}/soon.jpg"
    assert api["recent"][0]["poster_url"] == f"{proxy}/done.jpg"
    assert "image.tmdb.org" not in page
    assert page.count(f'src="{proxy}/') == 4
    # what completion detection goes on to store is untouched
    assert built == _PAYLOAD


def test_the_server_draws_what_is_ready_of_a_recent_show_as_the_polls_do(app_factory, conn):
    insert_admin_user(conn, username="person")
    app = app_factory(downloads_router, conn=conn)
    templates = Path(mediaman.__file__).parent / "web/templates"
    app.state.templates = Jinja2Templates(directory=str(templates))
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "person"))
    recent = [{**_item(""), "readiness": ready} for ready in ("part", "unknown", "ready")]
    built = {**deepcopy(_PAYLOAD), "recent": recent}
    with (
        patch("mediaman.web.routes.downloads.build_downloads_response", return_value=built),
        patch("mediaman.web.routes.downloads._in_library", return_value=True),
    ):
        grid = client.get("/downloads").text.split('id="dl-recent"')[1]

    assert re.findall(r'dl-recent-sub[^"]*">([^<]+)<', grid) == [
        "Some episodes ready",
        "Added",
        "Ready to watch",
    ]
    assert grid.count("dl-recent-sub--plain") == 2
    assert grid.count("dl-recent-badge") == 1
