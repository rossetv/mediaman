"""A viewer's Downloads page never offers a link into Radarr or Sonarr."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from tests.helpers.factories import insert_admin_user

_WEB = Path(mediaman.__file__).parent / "web"


def _item(item_id: str, **extra):
    return {
        "id": item_id,
        "title": "T " + item_id,
        "media_type": "movie",
        "state": "searching",
        "state_label": "Searching",
        "progress": 0,
        "abandon_visible": True,
        "stuck_seasons": [],
        "arr_id": 1,
        "arr_link": "http://radarr.internal:7878/movie/x",
        "arr_source": "Radarr",
        **extra,
    }


def _payload(*_args):
    return {
        "hero": _item("radarr:1"),
        "queue": [_item("sonarr:2", arr_source="Sonarr", media_type="tv")],
        "upcoming": [],
        "recent": [],
        "subtitle": "",
        "unreachable": [],
        "refused": [],
        "outage": "",
        "downloads_set_up": {"movies": True, "shows": True},
    }


def _page_and_api(app_factory, conn, role):
    insert_admin_user(conn, username="person", role=role)
    app = app_factory(downloads_router, conn=conn)
    app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")
    app.state.templates = Jinja2Templates(directory=str(_WEB / "templates"))
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", create_session(conn, "person"))
    with patch("mediaman.web.routes.downloads.build_downloads_response", side_effect=_payload):
        return client.get("/downloads").text, client.get("/api/downloads").json()


def _links(html: str) -> list[str]:
    return re.findall(r"<a [^>]*data-v=\"arr-link\"[^>]*>", html)


def test_a_viewer_is_given_no_link_into_radarr_or_sonarr(app_factory, conn):
    html, body = _page_and_api(app_factory, conn, "viewer")
    links = _links(html)
    assert links and all(" hidden" in tag and "href=" not in tag for tag in links)
    assert "radarr.internal" not in html
    assert [body["hero"], *body["queue"]] == [
        {**i, "arr_link": "", "arr_source": ""} for i in (body["hero"], *body["queue"])
    ]


def test_an_admin_is_given_the_link(app_factory, conn):
    html, _ = _page_and_api(app_factory, conn, "admin")
    assert any("href=" in tag for tag in _links(html))
