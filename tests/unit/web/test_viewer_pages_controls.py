"""Downloads and Recommended pages: a viewer is not shown the admin-only controls
(Abandon, Fetch new suggestions, the settings link, Share) and the page does not load
the script that calls an admin-only endpoint; an admin sees them all."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from mediaman.web.routes.recommended.pages import router as recommended_router
from tests.helpers.factories import insert_admin_user, insert_settings, insert_suggestion

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
}
_UPCOMING = {
    "id": "upcoming:9",
    "title": "Soon Show",
    "media_type": "tv",
    "release_label": "Soon",
    "state": "upcoming",
    "abandon_visible": False,
    "arr_id": 9,
}


def _payload(*_args):
    return {
        "hero": dict(_SEARCHING),
        "queue": [dict(_SEARCHING, id="radarr:2", title="Queued Movie")],
        "upcoming": [dict(_UPCOMING)],
        "recent": [],
        "subtitle": "",
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


def _get_recommended(client):
    with (
        patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
        patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
    ):
        return client.get("/recommended").text


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


def test_downloads_page_still_lists_the_items_for_a_viewer(make_client):
    html = _get_downloads(make_client(downloads_router, "viewer"))[0].text

    assert "Stuck Movie" in html
    assert "Queued Movie" in html
    assert "Soon Show" in html


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_recommended_admin_controls_follow_the_role(make_client, conn, role):
    insert_suggestion(conn, title="A Pick", tmdb_id=1)
    conn.commit()
    html = _get_recommended(make_client(recommended_router, role))
    shown = role == "admin"

    assert "A Pick" in html
    assert ("Fetch new suggestions" in html) is shown
    assert ("recommended/refresh.js" in html) is shown
    assert ('data-can-share="true"' in html) is shown


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_recommended_when_off_links_settings_only_for_an_admin(make_client, conn, role):
    insert_settings(conn, suggestions_enabled="false")
    conn.commit()
    html = _get_recommended(make_client(recommended_router, role))
    # The page nav has its own Settings link; look only at the disabled card.
    card = html.split("rec-disabled-sub", 1)[1].split("</div>", 1)[0]

    assert ("/settings" in card) is (role == "admin")
    assert ("Recommendations are off." in card) is (role == "viewer")


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_recommended_cooldown_span_is_for_admins_only(make_client, role):
    with patch(
        "mediaman.web.routes.recommended.pages.refresh_cooldown_remaining",
        return_value=timedelta(hours=3),
    ):
        html = _get_recommended(make_client(recommended_router, role))

    assert ("refresh-cooldown" in html) is (role == "admin")
