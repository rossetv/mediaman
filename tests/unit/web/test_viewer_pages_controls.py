"""Downloads page: a viewer is not shown the admin-only controls (Abandon) and the page
does not load the script that calls an admin-only endpoint; an admin sees them all."""

from __future__ import annotations

import json
import re
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

    def _make(router, role):
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
        return client.get("/downloads"), client.get("/api/downloads")


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_downloads_abandon_controls_follow_the_role(make_client, role):
    page, api = _get_downloads(make_client(downloads_router, role))
    html = page.text
    shown = role == "admin"

    assert page.status_code == 200
    assert ("data-abandon-trigger" in html) is shown
    assert ('id="abandon-modal"' in html) is shown
    assert ("dl-row-abandon" in html) is shown
    assert ('data-can-abandon="true"' in html) is shown
    body = api.json()
    assert body["hero"]["abandon_visible"] is shown
    assert body["queue"][0]["abandon_visible"] is shown


def test_each_abandon_button_names_the_very_title_it_is_of(make_client):
    html = _get_downloads(make_client(downloads_router, "admin"))[0].text
    assert re.findall(r'data-arr-id="(\d+)"', html) == ["1", "1", "9"]


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_where_radarr_and_sonarr_are_is_told_to_admins_alone(make_client, role):
    page, api = _get_downloads(make_client(downloads_router, role))
    body = api.json()
    items = [body["hero"], *body["queue"], *body["upcoming"]]
    told = [(i["arr_link"], i["arr_source"], i["arr_id"]) for i in items]

    if role == "admin":
        assert [t[2] for t in told] == [1, 1, 9]
        assert "radarr.internal" in page.text
    else:
        assert told == [("", "", 0)] * 3
        assert ".internal" not in page.text
        assert 'data-v="arr-link" hidden' in page.text


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_ids_and_dates_kept_for_completion_are_told_to_admins_alone(make_client, role):
    page, api = _get_downloads(make_client(downloads_router, role))
    body = api.json()
    items = [body["hero"], *body["queue"]]
    told = [(i.get("tmdb_id"), i.get("queued_since")) for i in items]

    if role == "admin":
        assert told == [(603, "2020-02-02T10:00:00+00:00")] * 2
    else:
        assert told == [(None, None)] * 2
        assert "2020-02-02" not in page.text


def test_downloads_page_still_lists_the_items_for_a_viewer(make_client):
    html = _get_downloads(make_client(downloads_router, "viewer"))[0].text

    assert "Stuck Movie" in html
    assert "Queued Movie" in html
    assert "Soon Show" in html


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_an_outage_is_shown_to_every_role_and_never_as_all_caught_up(make_client, role):
    line = "Can't reach Radarr and Sonarr"
    down = dict(_payload(), hero=None, queue=[], outage=line, subtitle=line)
    with patch("mediaman.web.routes.downloads.build_downloads_response", return_value=down):
        html = make_client(downloads_router, role).get("/downloads").text

    assert "All caught up" not in html
    assert "reach Radarr and Sonarr" in html


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_ring_on_the_downloads_page_begins_with_what_the_page_was_drawn_from(make_client, role):
    strip.on_page.clear()
    page, polled = _get_downloads(make_client(downloads_router, role))

    island = re.search(r'id="downloads-page-data">(.*?)</script>', page.text, re.S)
    assert island
    drawn = json.loads(island[1])["downloading"]
    assert drawn["summary"]["total"] > 0
    assert drawn["items"] == []
    assert drawn["summary"] == polled.json()["strip"]["summary"]
