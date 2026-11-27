"""Downloads: a viewer is told nothing of the admin-only controls (Abandon) and Watch does not
load the script that calls an admin-only endpoint; an admin is told them all."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman
from mediaman.services.downloads import strip
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from mediaman.web.routes.watch.pages import router as watch_router
from tests.helpers.factories import insert_admin_user

_WEB = Path(mediaman.__file__).parent / "web"

_SEARCHING = {
    "id": "radarr:1",
    "title": "Stuck Movie",
    "media_type": "movie",
    "state": "searching",
    "state_label": "Searching",
    "progress": 0,
    "abandon_visible": True,
    "stuck_seasons": [],
    "arr_id": 1,
    "arr_link": "http://radarr.internal:7878/movie/stuck-movie",
    "arr_source": "Radarr",
    "tmdb_id": 603,
    "queued_since": "2020-02-02T10:00:00+00:00",
}
_UPCOMING = {
    "id": "upcoming:9",
    "title": "Soon Show",
    "media_type": "tv",
    "release_label": "Soon",
    "state": "upcoming",
    "abandon_visible": False,
    "arr_id": 9,
    "arr_link": "http://sonarr.internal:8989/series/soon-show",
    "arr_source": "Sonarr",
}


def _payload(*_args):
    return {
        "hero": dict(_SEARCHING),
        "queue": [dict(_SEARCHING, id="radarr:2", title="Queued Movie")],
        "upcoming": [dict(_UPCOMING)],
        "recent": [],
        "subtitle": "",
        "unreachable": [],
        "refused": [],
        "outage": "",
        "downloads_set_up": {"movies": True, "shows": True},
    }


@pytest.fixture
def make_client(app_factory, conn):
    """A client logged in as a user of the given role, against the real templates."""

    def _make(router, role, *, again=False):
        if not again:
            insert_admin_user(conn, username="person", role=role)
        app = app_factory(router, conn=conn)
        app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")
        app.state.templates = Jinja2Templates(directory=str(_WEB / "templates"))
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", create_session(conn, "person"))
        return client

    return _make


def _get_downloads(client):
    with patch("mediaman.web.routes.downloads.build_downloads_response", side_effect=_payload):
        return client.get("/api/downloads")


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_downloads_abandon_is_offered_by_the_api_and_on_watch_to_an_admin_alone(make_client, role):
    api = _get_downloads(make_client(downloads_router, role))
    watch = make_client(watch_router, role, again=True).get("/")
    shown = role == "admin"

    body = api.json()
    assert body["hero"]["abandon_visible"] is shown
    assert body["queue"][0]["abandon_visible"] is shown
    assert watch.status_code == 200
    assert ('id="abandon-modal"' in watch.text) is shown
    assert ("/static/js/dl-abandon.js" in watch.text) is shown


def test_each_item_an_admin_may_abandon_carries_the_number_of_its_very_title(make_client):
    body = _get_downloads(make_client(downloads_router, "admin")).json()

    items = [body["hero"], *body["queue"], *body["upcoming"]]
    assert [(i["title"], i["arr_id"]) for i in items] == [
        ("Stuck Movie", 1),
        ("Queued Movie", 1),
        ("Soon Show", 9),
    ]


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_where_radarr_and_sonarr_are_is_told_to_admins_alone(make_client, role):
    api = _get_downloads(make_client(downloads_router, role))
    body = api.json()
    items = [body["hero"], *body["queue"], *body["upcoming"]]
    told = [(i["arr_link"], i["arr_source"], i["arr_id"]) for i in items]

    if role == "admin":
        assert [t[2] for t in told] == [1, 1, 9]
        assert "radarr.internal" in api.text
    else:
        assert told == [("", "", 0)] * 3
        assert ".internal" not in api.text


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_ids_and_dates_kept_for_completion_are_told_to_admins_alone(make_client, role):
    api = _get_downloads(make_client(downloads_router, role))
    body = api.json()
    items = [body["hero"], *body["queue"]]
    told = [(i.get("tmdb_id"), i.get("queued_since")) for i in items]

    if role == "admin":
        assert told == [(603, "2020-02-02T10:00:00+00:00")] * 2
    else:
        assert told == [(None, None)] * 2
        assert "2020-02-02" not in api.text


def test_the_downloads_api_still_lists_the_items_for_a_viewer(make_client):
    body = _get_downloads(make_client(downloads_router, "viewer")).json()

    titles = [i["title"] for i in (body["hero"], *body["queue"], *body["upcoming"])]
    assert titles == ["Stuck Movie", "Queued Movie", "Soon Show"]


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_downloads_api_tells_every_role_of_an_outage_and_never_all_caught_up(make_client, role):
    line = "Can't reach Radarr and Sonarr"
    down = dict(_payload(), hero=None, queue=[], outage=line, subtitle=line)
    with patch("mediaman.web.routes.downloads.build_downloads_response", return_value=down):
        api = make_client(downloads_router, role).get("/api/downloads")

    body = api.json()
    assert "caught up" not in api.text.lower()
    if role == "admin":
        assert body["outage"] == line
    else:
        assert body["outage"] == "Downloads aren't available"
        assert "Radarr and Sonarr" not in api.text


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_downloads_api_names_a_refused_key_to_an_admin_alone(make_client, role):
    line = "Can't sign in to Radarr"
    down = dict(_payload(), hero=None, queue=[], outage=line, subtitle=line, refused=["Radarr"])
    with patch("mediaman.web.routes.downloads.build_downloads_response", return_value=down):
        body = make_client(downloads_router, role).get("/api/downloads").json()

    if role == "admin":
        assert (body["outage"], body["refused"]) == (line, ["Radarr"])
    else:
        assert body["refused"] == [] and body["unreachable"] == []
        assert "Radarr" not in json.dumps(body)


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_downloads_api_tells_the_ring_what_it_read_without_its_items(make_client, role):
    strip.on_page.clear()
    client = make_client(downloads_router, role)
    first, again = _get_downloads(client).json(), _get_downloads(client).json()

    told = first["strip"]
    assert told["summary"]["total"] > 0
    assert told["items"] == []
    assert told["summary"] == again["strip"]["summary"]
